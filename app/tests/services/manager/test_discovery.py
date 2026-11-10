import asyncio

import pytest
import yaml

import config
from services.cloudflare import Cloudflare
from services.db.models import Container, Domain, Worker
from services.domains.decisions import ApplyOutcome
from services.domains.state import Domains
from services.manager.discovery import DiscoveryMixin
from services.tailscale import Tailscale
from services.workers.state import Workers
from tests.services.manager.test_domain_apply import DYNAMIC, HOME, Hosts
from tests.services.manager.test_domain_apply import Manager as DomainsManager
from tests.services.manager.test_domain_apply import fingerprint, queued
from utils.executor import active_executor
from utils.logging import TaskFailed


class DiscoveryHosts(Hosts):
  """The workers as discovery sees them: their stamps read back, their discovery files listed, several files removed
  in one command, and each one's address."""

  def __init__(self):
    super().__init__()
    self.addresses = {}

  def address(self, hostname):
    return self.addresses.get(hostname)

  async def exec_command(self, hostname, command, timeout=300):
    if command.startswith("grep -H . "):
      self.commands.append((hostname, "revisions"))
      return 0, [f"{path}:{content}" for (host, path), content in self.files.items()
                 if host == hostname and path.startswith(f"{HOME}/revisions/")]
    if command.startswith("find "):
      self.commands.append((hostname, "find"))
      return 0, sorted(path.rsplit("/", 1)[1] for host, path in self.files
                       if host == hostname and path.startswith(f"{DYNAMIC}/_discovery-"))
    if command.startswith("rm -f ") and " " in command[len("rm -f "):]:
      self.commands.append((hostname, command))
      for path in command[len("rm -f "):].split():
        self.files.pop((hostname, path), None)
      return 0, []
    return await super().exec_command(hostname, command, timeout)

  def discovery(self, hostname):
    """The worker's discovery files by name, parsed."""
    return {path.rsplit("/", 1)[1]: yaml.safe_load(content) for (host, path), content in self.files.items()
            if host == hostname and path.startswith(f"{DYNAMIC}/_discovery-")}

  def endpoints(self, hostname, name):
    http = self.discovery(hostname)[name]["http"]
    [middleware] = http["middlewares"].values()
    return middleware["headers"]["customResponseHeaders"]["Sage-Endpoints"]

  def stamp(self, hostname):
    return self.files.get((hostname, f"{HOME}/revisions/discovery"))


class Zone:
  """Cloudflare's zone as discovery's DNS sees it: its records, the listings taken and a failure (`fails`)."""

  def __init__(self):
    self.domain, self.tunnel_id, self.records, self.listings, self.fails = "example.test", "tunnel-platform", [], 0, None

  def connect(self):
    pass

  def get_dns_records(self, name=None):
    self.listings += 1
    if self.fails:
      raise Exception(self.fails)
    return [dict(record) for record in self.records]

  def sync_dns_records(self, records, zone=None, **fields):
    for comment, wanted in records.items():
      self.records = [record for record in self.records if record["comment"] != comment] + [
          {"comment": comment, "name": name, "content": content, **fields} for name, content in wanted.items()]

  def held(self):
    return sorted((record["comment"], record["name"], record["content"], record.get("type"), record.get("proxied"))
                  for record in self.records)


class Manager(DiscoveryMixin, DomainsManager):
  pass


@pytest.fixture
def hosts(register):
  return register(Tailscale, DiscoveryHosts())


@pytest.fixture
def zone(register):
  return register(Cloudflare, Zone())


@pytest.fixture
def manager(hosts, zone, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", HOME)
  monkeypatch.setenv("DOMAIN", "example.test")
  manager = Manager()
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def workers(db, hosts, manager):
  for hostname, subdomain, address in (("w1", "red", "100.64.0.2"), ("w2", "blue", "100.64.0.3")):
    Worker.create(hostname=hostname, subdomain=subdomain)
    hosts.found[hostname], hosts.addresses[hostname] = "reachable", address
    Workers().record_setup(hostname, "succeeded", address)
    manager.ready[hostname] = True


@pytest.fixture
def shop(application, workers, manager):
  """A public `shop` service with a name on each worker, both applied."""
  domains = []
  for hostname, name in (("w1", "a"), ("w2", "b")):
    container = Container.create(application=application, worker=hostname)
    manager.routable[container.id] = True
    domains.append(Domain.create(name=name, type="public", port=80, container=container, service="shop"))
    applied(domains[-1])
  return domains


def applied(domain):
  Domains().record_outcome(domain, ApplyOutcome(fingerprint(domain), True))


def writes(hosts, hostname):
  return [command for host, command in hosts.commands if host == hostname and command not in ("revisions", "find")]


class TestRequests:
  def test_each_worker_reachable_and_set_up_gets_its_own_publication_and_the_dns_one_its_own(self, manager, hosts, workers):
    Worker.create(hostname="w3", subdomain="green")
    Workers().record_setup("w3", "succeeded")
    Worker.create(hostname="w4", subdomain="gold")
    hosts.found["w4"] = "reachable"
    manager.request_discovery()
    assert [(key, scopes, params, quiet) for key, scopes, params, _, quiet in queued(manager)] == [
        (("publish_discovery", "w1"), ["worker:w1:discovery"], {"hostname": "w1"}, True),
        (("publish_discovery", "w2"), ["worker:w2:discovery"], {"hostname": "w2"}, True),
        (("publish_discovery_dns",), ["discovery"], {}, True)]

  async def test_a_request_while_one_runs_gets_one_after_it(self, manager, hosts, shop):
    seen = []

    async def during():
      manager.request_discovery()
      manager.task_queue.dispatch_tick()
      seen.append(sorted(tuple(task["key"]) for task in manager.task_queue.snapshot()["queued"]))

    original = hosts.sync_file

    async def sync_file(hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
      if hostname == "w1" and not seen:
        await during()
      await original(hostname, src, dst, keys, formatter, timeout, mode)

    hosts.sync_file = sync_file
    manager.request_discovery()
    await manager.run()
    assert len(seen) == 1 and ("publish_discovery", "w1") in seen[0]
    assert [command for command in hosts.commands if command == ("w1", "revisions")] == [("w1", "revisions")] * 2

  async def test_a_worker_held_by_its_setup_waits_and_the_others_do_not(self, manager, hosts, shop):
    release = asyncio.Event()

    async def converge_worker(hostname):
      await release.wait()

    manager.add_task(task=converge_worker, key=("converge_worker", "w1"), scopes={"worker:w1"}, executor="app",
                     params={"hostname": "w1"})
    manager.task_queue.dispatch_tick()
    manager.request_discovery()
    manager.task_queue.dispatch_tick()
    while ("w2", "revisions") not in hosts.commands:
      await asyncio.sleep(0)
    assert [tuple(task["key"]) for task in manager.task_queue.snapshot()["queued"]] == [("publish_discovery", "w1")]
    release.set()
    await manager.run()
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}


class TestPublication:
  async def test_every_service_is_written_with_its_names_and_the_stamp_last(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online'
    assert writes(hosts, "w1")[-1] == f"write {HOME}/revisions/discovery" and hosts.stamp("w1") is not None

  async def test_a_name_that_can_not_be_used_is_listed_offline(self, manager, hosts, shop):
    manager.ready["w2"] = False
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online=?0'

  async def test_an_unlinked_name_with_its_pool_is_listed_offline_and_one_without_a_service_is_not(self, manager, hosts, shop):
    Domain.create(name="c", type="public", port=80, service="shop", pool="eu")
    Domain.create(name="d", type="public", port=80, container=shop[0].container)
    Domain.create(name="e", type="internal", port=80, service="db")
    await manager.publish_discovery("w1")
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == (
        '"a.example.test";online, "b.example.test";online, "c.example.test";pool="eu";online=?0')

  async def test_nothing_is_written_while_the_stamp_and_the_files_are_current(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    hosts.commands.clear()
    await manager.publish_discovery("w1")
    assert writes(hosts, "w1") == []

  async def test_a_change_of_availability_rewrites_the_files_and_the_stamp(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    before = hosts.stamp("w1")
    manager.routable[shop[1].container_id] = False
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online=?0'
    assert hosts.stamp("w1") != before

  async def test_a_file_gone_is_written_again_though_the_stamp_matches(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    hosts.files.pop(("w1", f"{DYNAMIC}/_discovery-public-shop.yml"))
    await manager.publish_discovery("w1")
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}

  async def test_a_stale_service_is_removed_after_the_new_ones_are_written(self, manager, hosts, shop):
    for name in ("_discovery-public-old.yml", "_discovery-internal-gone.yml"):
      hosts.files[("w1", f"{DYNAMIC}/{name}")] = "http: {}\n"
    hosts.files[("w1", f"{DYNAMIC}/domain-0123456789abcdef.yml")] = "# sage-domain\n"
    await manager.publish_discovery("w1")
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}
    assert ("w1", f"{DYNAMIC}/domain-0123456789abcdef.yml") in hosts.files
    assert writes(hosts, "w1") == [
        f"write {DYNAMIC}/_discovery-public-shop.yml",
        f"rm -f {DYNAMIC}/_discovery-internal-gone.yml {DYNAMIC}/_discovery-public-old.yml",
        f"write {HOME}/revisions/discovery"]

  @pytest.mark.parametrize("found, setup", [("unreachable", "succeeded"), ("reachable", "failed"), ("reachable", None)])
  async def test_a_worker_not_reachable_and_set_up_by_the_time_it_runs_is_left_alone(self, manager, hosts, shop, found, setup):
    hosts.found["w1"] = found
    Workers().record_setup("w1", setup)
    await manager.publish_discovery("w1")
    assert [command for command in hosts.commands if command[0] == "w1"] == []

  async def test_a_worker_removed_before_it_runs_is_left_alone(self, manager, hosts, shop):
    Worker.delete().where(Worker.hostname == "w3").execute()
    await manager.publish_discovery("w3")
    assert hosts.commands == []

  async def test_a_failed_write_fails_the_task_and_writes_no_stamp(self, manager, hosts, shop):
    hosts.fails = "_discovery-public-shop.yml"
    with pytest.raises(TaskFailed):
      await manager.publish_discovery("w1")
    assert hosts.stamp("w1") is None


class TestDns:
  async def test_the_internal_name_points_at_every_worker_reachable_and_set_up_and_the_public_one_at_the_platform(
          self, manager, hosts, zone, workers):
    manager.ready["w2"] = False
    Worker.create(hostname="w3", subdomain="green")
    hosts.found["w3"], hosts.addresses["w3"] = "unreachable", "100.64.0.4"
    zone.records = [
        {"comment": "sage-discovery-w3", "name": "discovery.int.example.test", "content": "100.64.0.4", "type": "A", "proxied": False},
        {"comment": "sage-discovery-gone", "name": "discovery.int.example.test", "content": "100.64.0.9", "type": "A", "proxied": False},
        {"comment": "sage-worker-w1", "name": "*.red.int.example.test", "content": "100.64.0.2", "type": "A", "proxied": False},
        {"comment": "sage-tunnel", "name": "old.example.test", "content": "x.cfargotunnel.com", "type": "CNAME", "proxied": True}]
    await manager.publish_discovery_dns()
    assert zone.held() == [
        ("sage-discovery-w1", "discovery.int.example.test", "100.64.0.2", "A", False),
        ("sage-discovery-w2", "discovery.int.example.test", "100.64.0.3", "A", False),
        ("sage-tunnel", "discovery.example.test", "tunnel-platform.cfargotunnel.com", "CNAME", True),
        ("sage-worker-w1", "*.red.int.example.test", "100.64.0.2", "A", False)]
    assert zone.listings == 1

  async def test_a_worker_without_an_address_has_no_record(self, manager, hosts, zone, workers):
    hosts.addresses.pop("w2")
    await manager.publish_discovery_dns()
    assert [name for comment, name, *_ in zone.held() if comment == "sage-discovery-w2"] == []

  async def test_a_failure_fails_the_task(self, manager, zone, workers):
    zone.fails = "rate limited"
    with pytest.raises(TaskFailed):
      await manager.publish_discovery_dns()
