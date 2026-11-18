import asyncio
from datetime import timedelta

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

MANAGER = "https://manager.core.example.test"


class DiscoveryHosts(Hosts):
  """The workers as discovery sees them: the first line and the `Sage-Endpoints` line of each of their discovery files
  listed, several files removed in one command, and each one's address."""

  def __init__(self):
    super().__init__()
    self.addresses = {}

  def address(self, hostname):
    return self.addresses.get(hostname)

  async def exec_command(self, hostname, command, timeout=300):
    if command.startswith("grep -H -m1 ") and "/_discovery-" in command:
      self.commands.append((hostname, "list"))
      files = sorted((path, content) for (host, path), content in self.files.items()
                     if host == hostname and path.startswith(f"{DYNAMIC}/_discovery-"))
      return 0, [f"{path}:{content.splitlines()[0] if content else ''}" for path, content in files] + [
          f"{path}:{line}" for path, content in files for line in content.splitlines() if "Sage-Endpoints: " in line]
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

  def proof(self, hostname, name):
    """What the first line of the worker's discovery file `name` names."""
    return self.files[(hostname, f"{DYNAMIC}/{name}")].splitlines()[0]


class Zone:
  """Cloudflare's zone as discovery's DNS sees it: its records, the listings taken, a failure (`fails`) and each worker
  tunnel's status as known now (`tunnels`, by hostname)."""

  def __init__(self):
    self.domain, self.tunnel_id, self.records, self.listings, self.fails = "example.test", "tunnel-platform", [], 0, None
    self.manager_hostname, self.tunnels = "manager.core.example.test", {}

  def connect(self):
    pass

  def worker_tunnel_name(self, hostname):
    return hostname

  def tunnel(self, name):
    return self.tunnels.get(name)

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
  return [command for host, command in hosts.commands if host == hostname and command != "list"]


class TestRequests:
  def test_each_worker_reachable_and_set_up_gets_its_own_publication_and_the_dns_one_its_own(self, manager, hosts, workers):
    Worker.create(hostname="w3", subdomain="green")
    Workers().record_setup("w3", "succeeded")
    Worker.create(hostname="w4", subdomain="gold")
    hosts.found["w4"] = "reachable"
    manager.request_discovery()
    assert [(key, scopes, params, quiet) for key, scopes, params, _, quiet in queued(manager)] == [
        (("publish_discovery", "w1"), ["worker:w1:discovery"], {"hostname": "w1", "force": False}, True),
        (("publish_discovery", "w2"), ["worker:w2:discovery"], {"hostname": "w2", "force": False}, True),
        (("publish_discovery_dns",), ["discovery"], {}, True)]

  def test_a_forced_request_names_one_worker_and_is_its_own_visible_task(self, manager, hosts, workers):
    manager.request_discovery()
    manager.request_discovery("w1", force=True)
    assert [(key, params.get("force"), quiet) for key, _, params, _, quiet in queued(manager)] == [
        (("publish_discovery", "w1"), False, True), (("publish_discovery", "w2"), False, True),
        (("publish_discovery", "w1", "force"), True, False), (("publish_discovery_dns",), None, True)]

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
    assert [command for command in hosts.commands if command == ("w1", "list")] == [("w1", "list")] * 2

  async def test_a_worker_held_by_its_setup_waits_and_the_others_do_not(self, manager, hosts, shop):
    release = asyncio.Event()

    async def converge_worker(hostname):
      await release.wait()

    manager.add_task(task=converge_worker, key=("converge_worker", "w1"), scopes={"worker:w1"}, executor="app",
                     params={"hostname": "w1"})
    manager.task_queue.dispatch_tick()
    manager.request_discovery()
    manager.task_queue.dispatch_tick()
    while ("w2", "list") not in hosts.commands:
      await asyncio.sleep(0)
    assert [tuple(task["key"]) for task in manager.task_queue.snapshot()["queued"]] == [("publish_discovery", "w1")]
    release.set()
    await manager.run()
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}


class TestPublication:
  async def test_every_service_is_written_with_its_names_and_its_proof_in_its_first_line(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online'
    assert hosts.proof("w1", "_discovery-public-shop.yml").startswith("# sage-discovery ")
    assert writes(hosts, "w1") == [f"write {DYNAMIC}/_discovery-public-shop.yml"]

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

  async def test_nothing_is_written_while_every_files_proof_is_current(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    hosts.commands.clear()
    await manager.publish_discovery("w1")
    assert writes(hosts, "w1") == []

  async def test_a_forced_publication_writes_them_though_they_are_current(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    hosts.commands.clear()
    await manager.publish_discovery("w1", force=True)
    assert writes(hosts, "w1") == [f"write {DYNAMIC}/_discovery-public-shop.yml"]

  async def test_a_change_of_availability_rewrites_the_file_with_its_new_proof(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    before = hosts.proof("w1", "_discovery-public-shop.yml")
    manager.routable[shop[1].container_id] = False
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online=?0'
    assert hosts.proof("w1", "_discovery-public-shop.yml") != before

  async def test_a_file_gone_or_without_its_proof_is_written_again(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    hosts.files.pop(("w1", f"{DYNAMIC}/_discovery-public-shop.yml"))
    await manager.publish_discovery("w1")
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}
    hosts.files[("w1", f"{DYNAMIC}/_discovery-public-shop.yml")] = "http: {}\n"
    hosts.commands.clear()
    await manager.publish_discovery("w1")
    assert writes(hosts, "w1") == [f"write {DYNAMIC}/_discovery-public-shop.yml"]

  async def test_a_stale_service_is_removed_after_the_new_ones_are_written(self, manager, hosts, shop):
    for name in ("_discovery-public-old.yml", "_discovery-internal-gone.yml"):
      hosts.files[("w1", f"{DYNAMIC}/{name}")] = "http: {}\n"
    hosts.files[("w1", f"{DYNAMIC}/domain-0123456789abcdef.yml")] = "# sage-domain\n"
    await manager.publish_discovery("w1")
    assert set(hosts.discovery("w1")) == {"_discovery-public-shop.yml"}
    assert ("w1", f"{DYNAMIC}/domain-0123456789abcdef.yml") in hosts.files
    assert writes(hosts, "w1") == [
        f"write {DYNAMIC}/_discovery-public-shop.yml",
        f"rm -f {DYNAMIC}/_discovery-internal-gone.yml {DYNAMIC}/_discovery-public-old.yml"]

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

  async def test_a_failed_write_fails_the_task(self, manager, hosts, shop, notices):
    hosts.fails = "_discovery-public-shop.yml"
    with pytest.raises(TaskFailed):
      await manager.publish_discovery("w1")
    assert hosts.discovery("w1") == {}
    [(level, content, link)] = notices()
    assert (level, content.startswith("Discovery could not be published on worker w1: Error writing"), link) == (
        "error", True, f"{MANAGER}/workers/w1")


class TestNotKnownYet:
  @pytest.fixture
  def db_name(self, shop):
    """A `db` service with one name, on w1, applied; `shop` has a name on each worker."""
    domain = Domain.create(name="db", type="public", port=5432, container=shop[0].container, service="db")
    applied(domain)
    return domain

  async def test_a_name_not_known_yet_keeps_its_published_availability_while_a_known_one_changes(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    manager.routable[shop[1].container_id] = None
    manager.routable[shop[0].container_id] = False
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online=?0, "b.example.test";online'

  async def test_a_domain_added_to_a_service_with_a_name_not_known_yet_is_listed_at_once(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    manager.ready["w2"] = None
    applied(Domain.create(name="c", type="public", port=80, container=shop[0].container, service="shop"))
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == (
        '"a.example.test";online, "b.example.test";online, "c.example.test";online')

  async def test_a_name_not_known_yet_that_the_worker_does_not_publish_is_offline(self, manager, hosts, shop):
    manager.ready["w2"] = None
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online=?0'

  async def test_a_forced_publication_keeps_a_published_availability_too(self, manager, hosts, shop, db_name):
    await manager.publish_discovery("w1")
    manager.ready["w2"] = None
    manager.routable[shop[0].container_id] = False
    await manager.publish_discovery("w1", force=True)
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online=?0, "b.example.test";online'
    assert hosts.endpoints("w1", "_discovery-public-db.yml") == '"db.example.test";online=?0'

  async def test_an_apply_under_way_keeps_its_names_published_availability(self, manager, hosts, shop):
    await manager.publish_discovery("w1")
    manager.request_domain_applies([shop[1].id])
    manager.routable[shop[0].container_id] = False
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online=?0, "b.example.test";online'

  async def test_a_name_not_known_is_offline_once_the_manager_has_run_for_the_hold(self, clock, manager, hosts, shop):
    await manager.publish_discovery("w1")
    manager.ready["w2"] = None
    clock.advance(timedelta(seconds=config.UNKNOWN_HOLD_SECONDS - 1))
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online'
    clock.advance(timedelta(seconds=1))
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online=?0'

  async def test_an_apply_under_way_is_held_from_when_it_was_queued_and_offline_after_the_hold(self, clock, manager, hosts, shop):
    await manager.publish_discovery("w1")
    clock.advance(timedelta(minutes=30))
    manager.request_domain_applies([shop[1].id])
    clock.advance(timedelta(seconds=config.UNKNOWN_HOLD_SECONDS - 1))
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online'
    clock.advance(timedelta(seconds=1))
    await manager.publish_discovery("w1")
    assert hosts.endpoints("w1", "_discovery-public-shop.yml") == '"a.example.test";online, "b.example.test";online=?0'

  async def test_after_a_restart_nothing_is_rewritten_while_not_known_nor_once_known_again(self, manager, hosts, shop):
    for hostname in ("w1", "w2"):
      await manager.publish_discovery(hostname)
    published = {key: content for key, content in hosts.files.items() if "/_discovery-" in key[1]}
    Domains().clear()
    hosts.commands.clear()
    manager.request_discovery()
    await manager.run()
    for domain in shop:
      applied(domain)
    manager.request_discovery()
    await manager.run()
    assert {key: content for key, content in hosts.files.items() if "/_discovery-" in key[1]} == published
    assert writes(hosts, "w1") == writes(hosts, "w2") == []


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

  @pytest.mark.parametrize("found, setup, kept", [
      ("unobserved", None, True), ("unknown", "succeeded", True), ("reachable", None, True),
      ("unreachable", None, False), ("reachable", "failed", False), ("reachable", "refused", False)])
  async def test_a_worker_known_to_be_out_loses_its_record_and_one_not_known_yet_keeps_it_as_it_is(
          self, manager, hosts, zone, workers, found, setup, kept):
    zone.records = [{"comment": "sage-discovery-w2", "name": "discovery.int.example.test", "content": "100.64.0.9",
                     "type": "A", "proxied": False}]
    hosts.found["w2"] = found
    Workers().record_setup("w2", setup)
    await manager.publish_discovery_dns()
    assert [content for comment, _, content, *_ in zone.held() if comment == "sage-discovery-w2"] == (
        ["100.64.0.9"] if kept else [])

  @pytest.mark.parametrize("found, tunnel, kept", [("unreachable", "healthy", True), ("unreachable", "down", False),
                                                   ("unknown", "healthy", True), ("unknown", "down", False)])
  async def test_a_worker_answering_only_through_its_tunnel_keeps_its_record_and_one_answering_neither_way_loses_it(
          self, clock, manager, hosts, zone, workers, found, tunnel, kept):
    zone.records = [{"comment": "sage-discovery-w2", "name": "discovery.int.example.test", "content": "100.64.0.9",
                     "type": "A", "proxied": False}]
    hosts.found["w2"], zone.tunnels["w2"] = found, tunnel
    Workers().record_setup("w2", None if found == "unreachable" else "succeeded")
    clock.advance(timedelta(seconds=config.UNKNOWN_HOLD_SECONDS))
    await manager.publish_discovery_dns()
    assert [content for comment, _, content, *_ in zone.held() if comment == "sage-discovery-w2"] == (
        ["100.64.0.9"] if kept else [])

  @pytest.mark.parametrize("found, setup, kept", [("unobserved", None, False), ("reachable", None, False),
                                                  ("unknown", "succeeded", True)])
  async def test_two_minutes_after_the_start_a_worker_still_not_known_is_out_unless_the_manager_is_cut_off(
          self, clock, manager, hosts, zone, workers, found, setup, kept):
    zone.records = [{"comment": "sage-discovery-w2", "name": "discovery.int.example.test", "content": "100.64.0.9",
                     "type": "A", "proxied": False}]
    hosts.found["w2"] = found
    Workers().record_setup("w2", setup)
    clock.advance(timedelta(seconds=config.UNKNOWN_HOLD_SECONDS))
    await manager.publish_discovery_dns()
    assert [content for comment, _, content, *_ in zone.held() if comment == "sage-discovery-w2"] == (
        ["100.64.0.9"] if kept else [])

  async def test_after_a_restart_no_record_goes_until_its_worker_is_known(self, manager, hosts, zone, workers):
    zone.records = [{"comment": f"sage-discovery-{hostname}", "name": "discovery.int.example.test", "content": address,
                     "type": "A", "proxied": False} for hostname, address in (("w1", "100.64.0.2"), ("w2", "100.64.0.3"))]
    Workers().clear()
    hosts.found.clear()
    manager.request_discovery()
    await manager.run()
    assert sorted(comment for comment, *_ in zone.held() if comment.startswith("sage-discovery-")) == [
        "sage-discovery-w1", "sage-discovery-w2"]
    hosts.found.update(w1="reachable", w2="unreachable")
    Workers().record_setup("w1", "succeeded", "100.64.0.2")
    manager.request_discovery()
    await manager.run()
    assert sorted(comment for comment, *_ in zone.held() if comment.startswith("sage-discovery-")) == ["sage-discovery-w1"]

  async def test_a_failure_fails_the_task(self, manager, zone, workers, notices):
    zone.fails = "rate limited"
    with pytest.raises(TaskFailed):
      await manager.publish_discovery_dns()
    assert notices() == [("error", "The discovery DNS records could not be published: rate limited", f"{MANAGER}/domains")]
