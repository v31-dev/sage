import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import config
from services.cloudflare import Cloudflare
from services.containers.decisions import ContainerView
from services.db.models import Container, Domain, Worker
from services.domains.config import DomainConfig, parse_route_listing
from services.domains.decisions import ApplyOutcome, RouteObservation
from services.domains.state import Domains
from services.manager.domains import DomainsMixin
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from services.updates import UpdateSource
from services.workers.state import Workers
from utils.executor import active_executor
from utils.queue import TaskQueue

HOME = "/opt/sage"
DYNAMIC = f"{HOME}/traefik/dynamic"

MANAGER = "https://manager.core.example.test"


class Hosts:
  """The workers as an apply sees them: each one's SSH fact, its files by path, the commands run on it, and a command
  that fails (`fails`, a part of it); `during` runs once, at the first write."""

  def __init__(self):
    self.found, self.files, self.commands, self.fails, self.during = {}, {}, [], None, None

  def ssh(self, hostname):
    return self.found.get(hostname, "unobserved")

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append((hostname, command))
    if self.fails and self.fails in command:
      raise Exception(f"Error executing command on {hostname}: exit code 1")
    if command.startswith("rm -f "):
      self.files.pop((hostname, command[len("rm -f "):]), None)
    return 0, []

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    self.commands.append((hostname, f"write {dst}"))
    if self.fails and self.fails in dst:
      raise Exception(f"Error writing {dst} on {hostname}")
    if self.during:
      during, self.during = self.during, None
      during()
    self.files[(hostname, dst)] = keys["CONTENT"]

  def route(self, hostname, domain):
    """The domain id and fingerprint the routing file's first line names, None without a file."""
    content = self.files.get((hostname, f"{DYNAMIC}/domain-{domain.id}.yml"))
    if content is None:
      return None
    return parse_route_listing([f"{DYNAMIC}/domain-{domain.id}.yml:{content.splitlines()[0]}"])[domain.id]


class Provider:
  """Cloudflare as an apply uses it: the zone's records by comment, each worker's tunnel, and a failure (`fails`)."""

  def __init__(self):
    self.domain, self.zone, self.tunnels, self.fails, self.calls = "example.test", {}, {}, None, []
    self.manager_hostname = "manager.core.example.test"

  def connect(self):
    pass

  def worker_tunnel_name(self, hostname):
    return f"sage-{self.domain}-{hostname}"

  def find_tunnel(self, name):
    return self.tunnels.get(name)

  def sync_dns_records(self, records, zone=None, **fields):
    self.calls.append((records, fields))
    if self.fails:
      raise Exception(self.fails)
    for comment, wanted in records.items():
      self.zone[comment] = dict(wanted)


class Manager(DomainsMixin, QueueMixin):
  """The queue and the domains' part of the Manager; the containers' `routable` and the workers' readiness are what
  the test sets."""

  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.routable, self.ready, self.finished, self.discovered = {}, {}, [], 0
    self.queue_facts = self.domains_queue_facts()
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool}, record=lambda task, status: self.finished.append((task.name, status)),
                                notify=self._queue_changed)

  def request_discovery(self):
    self.discovered += 1

  def container_states(self, containers):
    return {container.id: SimpleNamespace(routable=self.routable.get(container.id), view=ContainerView(
        "active" if self.routable.get(container.id) else "inactive", "current")) for container in containers}

  def worker_states(self, hostnames=None):
    return {hostname: SimpleNamespace(view=SimpleNamespace(ready=self.ready.get(hostname))) for hostname in hostnames}

  async def run(self):
    """Runs what is queued, and what that queues, to the end."""
    self.task_queue.dispatch_tick()
    while self.task_queue.snapshot()["running"] or self.task_queue.snapshot()["queued"]:
      await asyncio.sleep(0)
      self.task_queue.dispatch_tick()


@pytest.fixture
def hosts(register):
  return register(Tailscale, Hosts())


@pytest.fixture
def provider(register):
  provider = register(Cloudflare, Provider())
  provider.tunnels = {"sage-example.test-w1": {"id": "t1"}, "sage-example.test-w2": {"id": "t2"}}
  return provider


@pytest.fixture
def manager(hosts, provider, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", HOME)
  monkeypatch.setenv("DOMAIN", "example.test")
  manager = Manager()
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def workers(db, hosts):
  for hostname, subdomain in (("w1", "red"), ("w2", "blue")):
    Worker.create(hostname=hostname, subdomain=subdomain)
    hosts.found[hostname] = "reachable"
    Workers().record_setup(hostname, "succeeded", "100.64.0.2")


@pytest.fixture
def container(application, workers, manager):
  container = Container.create(application=application, worker="w1")
  manager.routable[container.id], manager.ready["w1"] = True, True
  return container


@pytest.fixture
def public(container):
  return Domain.create(name="shop", type="public", port=8080, container=container)


@pytest.fixture
def internal(container):
  return Domain.create(name="api", type="internal", port=80, container=container, namespace_worker="w1")


def fingerprint(domain):
  return DomainConfig.capture(Domain.get_by_id(domain.id), "example.test").content_hash


def state(manager, domain):
  return manager.domain_states([Domain.get_by_id(domain.id)])[domain.id]


def queued(manager):
  return [(tuple(task["key"]), task["scopes"], task["params"], task["labels"], task["quiet"])
          for task in manager.task_queue.snapshot()["queued"]]


async def applied(manager, domain):
  manager.request_domain_applies([domain.id])
  await manager.run()
  return state(manager, domain)


class TestRequests:
  def test_a_linked_public_domain_holds_itself_its_name_and_its_route_on_its_worker(self, manager, public):
    manager.request_domain_applies([public.id])
    assert queued(manager) == [(
        ("apply_domain", public.id), sorted({f"domain:{public.id}", "domain:host:shop.example.test", f"worker:w1:routing:{public.id}"}),
        {"domain_id": public.id, "worker": "w1", "host": "shop.example.test"}, {"Domain": "shop.example.test"}, True)]

  def test_an_internal_domains_name_is_under_its_workers_subdomain(self, manager, internal):
    manager.request_domain_applies([internal.id])
    [(_, scopes, params, labels, _)] = queued(manager)
    assert "domain:host:api.red.int.example.test" in scopes and params["host"] == "api.red.int.example.test"

  def test_an_unlinked_public_domain_holds_no_worker(self, manager, public):
    Domain.update(container=None).where(Domain.id == public.id).execute()
    manager.request_domain_applies([public.id])
    [(_, scopes, params, _, _)] = queued(manager)
    assert scopes == sorted({f"domain:{public.id}", "domain:host:shop.example.test"}) and params["worker"] is None

  def test_the_latest_of_requests_waiting_is_the_one_kept(self, manager, public):
    manager.request_domain_applies([public.id])
    Domain.update(name="store").where(Domain.id == public.id).execute()
    manager.request_domain_applies([public.id])
    [(_, scopes, params, _, _)] = queued(manager)
    assert params["host"] == "store.example.test" and "domain:host:store.example.test" in scopes

  async def test_a_request_while_one_runs_waits_for_it_and_then_runs(self, manager, public, hosts):
    seen = []

    def again():
      manager.request_domain_applies([public.id])
      manager.task_queue.dispatch_tick()
      seen.append(tuple(len(tasks) for tasks in manager.task_queue.snapshot().values()))

    hosts.during = again
    await applied(manager, public)
    assert seen == [(1, 1)]
    assert [command for command in hosts.commands if command[1].startswith("write ")] == [
        ("w1", f"write {DYNAMIC}/domain-{public.id}.yml")] * 2

  def test_a_deleted_domain_is_not_requested(self, manager, public):
    Domain.delete().where(Domain.id == public.id).execute()
    manager.request_domain_applies([public.id])
    assert queued(manager) == []


class TestApply:
  async def test_a_routable_container_gets_its_route_and_its_name_points_at_its_workers_tunnel(self, manager, public, hosts, provider):
    view = (await applied(manager, public)).view
    assert hosts.route("w1", public) == fingerprint(public)
    assert provider.zone == {f"sage-domain-{public.id}": {"shop.example.test": "t1.cfargotunnel.com"}}
    assert provider.calls[0][1] == {"type": "CNAME", "proxied": True}
    assert (view.status, view.conforms) == ("synced", True)
    assert Domains().outcome(public.id) == ApplyOutcome(fingerprint(public), True)

  async def test_a_stopped_container_loses_its_route_and_keeps_its_name(self, manager, public, hosts, provider):
    await applied(manager, public)
    manager.routable[public.container_id] = False
    view = (await applied(manager, public)).view
    assert hosts.route("w1", public) is None
    assert provider.zone == {f"sage-domain-{public.id}": {"shop.example.test": "t1.cfargotunnel.com"}}
    assert (view.status, view.conforms) == ("synced", True)

  async def test_while_its_container_is_not_known_its_route_is_left_and_nothing_is_claimed(self, manager, public, hosts, provider):
    await applied(manager, public)
    Domain.update(port=9090).where(Domain.id == public.id).execute()
    manager.routable[public.container_id] = None
    view = (await applied(manager, public)).view
    assert hosts.route("w1", public) is not None and hosts.route("w1", public) != fingerprint(public)
    assert provider.zone == {f"sage-domain-{public.id}": {"shop.example.test": "t1.cfargotunnel.com"}}
    assert (view.status, view.reason) == ("pending", "eligibility_unknown")
    assert Domains().outcome(public.id).fingerprint != fingerprint(public)

  @pytest.mark.parametrize("found, setup", [("unreachable", "succeeded"), ("unknown", "succeeded"), ("reachable", None),
                                            ("reachable", "refused"), ("reachable", "failed")])
  async def test_a_worker_not_reachable_and_set_up_is_not_written_to(self, manager, public, hosts, provider, found, setup):
    hosts.found["w1"], manager.ready["w1"] = found, False
    Workers().record_setup("w1", setup)
    view = (await applied(manager, public)).view
    assert [command for command in hosts.commands if command[0] == "w1"] == []
    assert provider.zone == {f"sage-domain-{public.id}": {"shop.example.test": "t1.cfargotunnel.com"}}
    assert (view.status, view.reason) == ("pending", "worker_not_ready")
    assert Domains().outcome(public.id) is None

  async def test_an_internal_domain_has_no_public_name(self, manager, internal, hosts, provider):
    view = (await applied(manager, internal)).view
    assert hosts.route("w1", internal) == fingerprint(internal)
    assert provider.zone == {f"sage-domain-{internal.id}": {}}
    assert view.status == "synced"

  async def test_an_unlinked_internal_domain_loses_its_route_on_its_namespace_worker(self, manager, internal, hosts):
    await applied(manager, internal)
    Domain.update(container=None).where(Domain.id == internal.id).execute()
    view = (await applied(manager, internal)).view
    assert hosts.route("w1", internal) is None
    assert view.status == "synced"

  async def test_an_unlinked_public_domain_loses_its_name_and_touches_no_worker(self, manager, public, hosts, provider):
    await applied(manager, public)
    hosts.commands.clear()
    Domain.update(container=None).where(Domain.id == public.id).execute()
    view = (await applied(manager, public)).view
    assert hosts.commands == []
    assert provider.zone == {f"sage-domain-{public.id}": {}}
    assert view.status == "synced"

  async def test_a_failed_route_write_is_the_routes_error_and_dns_is_not_touched(self, manager, public, hosts, provider, notices):
    hosts.fails = f"domain-{public.id}.yml"
    view = (await applied(manager, public)).view
    [(level, content, link)] = notices()
    assert (level, content.startswith("Domain shop.example.test: its route could not be applied: Error writing"), link) == (
        "error", True, f"{MANAGER}/domains")
    assert provider.calls == []
    assert (view.status, view.reason) == ("error", "route")
    assert "Error writing" in view.error
    assert manager.finished == [("apply_domain", "failed")]

  async def test_a_failed_removal_is_the_routes_error(self, manager, public, hosts):
    manager.routable[public.container_id] = False
    hosts.fails = "rm -f"
    view = (await applied(manager, public)).view
    assert (view.status, view.reason) == ("error", "route")

  async def test_a_dns_failure_after_the_route_keeps_what_the_route_did(self, manager, public, hosts, provider, notices):
    provider.fails = "rate limited"
    view = (await applied(manager, public)).view
    assert notices() == [("error", "Domain shop.example.test: its DNS records could not be applied: rate limited", f"{MANAGER}/domains")]
    assert hosts.route("w1", public) == fingerprint(public)
    assert Domains().route(public.id, "w1") == RouteObservation("present", fingerprint(public))
    assert (view.status, view.reason, view.error) == ("error", "dns", "rate limited")

  async def test_a_worker_without_a_tunnel_is_a_dns_error(self, manager, public, provider):
    provider.tunnels = {}
    view = (await applied(manager, public)).view
    assert (view.status, view.reason, view.error) == ("error", "dns", "Worker w1 has no tunnel.")

  async def test_a_failed_apply_is_not_tried_again_by_itself(self, manager, public, provider):
    provider.fails = "rate limited"
    await applied(manager, public)
    assert (manager.finished, queued(manager)) == ([("apply_domain", "failed")], [])

  async def test_each_apply_republishes_discovery_failed_ones_included(self, manager, public, provider):
    await applied(manager, public)
    provider.fails = "rate limited"
    await applied(manager, public)
    assert manager.discovered == 2

  async def test_a_success_after_a_failure_replaces_it(self, manager, public, provider):
    provider.fails = "rate limited"
    await applied(manager, public)
    provider.fails = None
    assert (await applied(manager, public)).view.status == "synced"

  async def test_an_edit_during_the_apply_is_applied_after_it_and_the_old_result_does_not_count(self, manager, public, hosts):
    hosts.during = lambda: Domain.update(port=9090).where(Domain.id == public.id).execute()
    first = fingerprint(public)
    manager.request_domain_applies([public.id])
    manager.task_queue.dispatch_tick()
    while manager.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
    assert Domains().outcome(public.id) == ApplyOutcome(first, True)
    assert state(manager, public).view.status == "syncing"
    await manager.run()
    assert hosts.route("w1", public) == fingerprint(public) != first
    assert state(manager, public).view.status == "synced"

  async def test_a_domain_moved_before_its_apply_ran_is_requested_again_with_its_new_worker(self, manager, public, hosts, application):
    other = Container.create(application=application, worker="w2")
    manager.routable[other.id], manager.ready["w2"] = True, True
    manager.request_domain_applies([public.id])
    Domain.update(container=other).where(Domain.id == public.id).execute()
    await manager.run()
    assert hosts.route("w1", public) is None and hosts.route("w2", public) == fingerprint(public)
    assert [hostname for hostname, _ in hosts.commands] == ["w2"]

  async def test_a_domain_deleted_before_its_apply_ran_is_forgotten_and_nothing_is_touched(self, manager, public, hosts, provider):
    Domains().record_outcome(public, ApplyOutcome("f1", True))
    manager.request_domain_applies([public.id])
    Domain.delete().where(Domain.id == public.id).execute()
    await manager.run()
    assert (hosts.commands, provider.calls, Domains().outcome(public.id)) == ([], [], None)

  async def test_a_domain_deleted_during_its_apply_is_forgotten(self, manager, public, hosts):
    hosts.during = lambda: Domain.delete().where(Domain.id == public.id).execute()
    manager.request_domain_applies([public.id])
    await manager.run()
    assert (Domains().outcome(public.id), Domains().route(public.id, "w1")) == (None, RouteObservation("unobserved"))


class TestStates:
  def test_an_apply_queued_or_running_shows_the_domain_syncing_and_shows_it_again(self, manager, public, signals):
    assert state(manager, public).view.status == "pending"
    manager.request_domain_applies([public.id])
    assert state(manager, public).view.status == "syncing"
    assert (UpdateSource.DOMAINS, None) in signals and (UpdateSource.APPLICATION, "demo/web") in signals

  async def test_the_domains_end_shows_it_again(self, manager, public, signals):
    manager.request_domain_applies([public.id])
    signals.clear()
    await manager.run()
    assert (UpdateSource.DOMAINS, None) in signals and (UpdateSource.WORKER_CONTAINERS, "w1") in signals

  def test_each_domain_takes_its_own_containers_and_workers_facts(self, manager, public, internal, application):
    other = Container.create(application=application, worker="w2")
    moved = Domain.create(name="moved", type="public", port=80, container=other)
    manager.routable[other.id], manager.ready["w2"] = None, True
    manager.ready["w1"] = False
    states = manager.domain_states(Domain.select())
    assert {domain_id: (each.facts.eligible, each.facts.worker_ready) for domain_id, each in states.items()} == {
        public.id: (True, False), internal.id: (True, False), moved.id: (None, True)}


class TestPayloads:
  def test_each_domain_is_given_in_the_order_asked_with_its_state_now(self, manager, public, internal):
    manager.ready["w1"] = False
    payloads = manager.domain_payloads([Domain.get_by_id(public.id), Domain.get_by_id(internal.id)])
    assert [(payload["id"], payload["address"]) for payload in payloads] == [
        (public.id, "https://shop.example.test"), (internal.id, "https://api.red.int.example.test")]
    assert [payload["view"]["reason"] for payload in payloads] == ["worker_not_ready", "worker_not_ready"]

  def test_a_domain_whose_apply_is_queued_shows_it_syncing(self, manager, public):
    manager.request_domain_applies([public.id])
    [payload] = manager.domain_payloads([Domain.get_by_id(public.id)])
    assert payload["view"]["status"] == "syncing"

  def test_the_domains_page_has_every_domain_and_every_container_one_can_link_to_with_its_state(
          self, manager, public, internal, application):
    other = Container.create(application=application, worker="w2")
    page = manager.domains_page()
    assert [domain["name"] for domain in page["domains"]] == ["api", "shop"]
    assert [(item["id"], item["worker"], item["view"]["headline"]) for item in page["containers"]] == [
        (public.container_id, {"hostname": "w1", "subdomain": "red"}, "active"),
        (other.id, {"hostname": "w2", "subdomain": "blue"}, "inactive")]
