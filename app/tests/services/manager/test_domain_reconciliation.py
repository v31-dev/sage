import pytest

import config
from services.cloudflare import Cloudflare
from services.db.models import Container, Domain, Worker
from services.domains.config import DomainConfig
from services.domains.decisions import ApplyOutcome, RouteObservation
from services.domains.state import Domains
from services.tailscale import Tailscale
from services.workers.state import Workers
from tests.services.manager.test_domain_apply import DYNAMIC, HOME, Hosts, Manager, Provider, fingerprint, queued, state
from utils.executor import active_executor
from utils.logging import TaskFailed

GONE = "0123456789abcdef"

MANAGER = "https://manager.core.example.test"


class ListedHosts(Hosts):
  """The workers as the pass sees them too: their routing files listed by first line, several removed in one command,
  a listing that fails (`listing_fails`, a hostname) and `while_listing`, run once, as the first listing is taken."""

  def __init__(self):
    super().__init__()
    self.listing_fails, self.while_listing = None, None

  async def exec_command(self, hostname, command, timeout=300):
    if command.startswith("grep -H -m1 "):
      self.commands.append((hostname, "list"))
      if self.while_listing:
        while_listing, self.while_listing = self.while_listing, None
        while_listing()
      if hostname == self.listing_fails:
        raise Exception(f"Error executing command on {hostname}: timed out")
      return 0, [f"{path}:{content.splitlines()[0]}" for (host, path), content in sorted(self.files.items())
                 if host == hostname and path.startswith(f"{DYNAMIC}/domain-")]
    if command.startswith("rm -f ") and " " in command[len("rm -f "):]:
      self.commands.append((hostname, command))
      if self.fails and self.fails in command:
        raise Exception(f"Error executing command on {hostname}: exit code 1")
      for path in command[len("rm -f "):].split():
        self.files.pop((hostname, path), None)
      return 0, []
    return await super().exec_command(hostname, command, timeout)

  def put(self, hostname, domain_id, fingerprint):
    self.files[(hostname, f"{DYNAMIC}/domain-{domain_id}.yml")] = f"# sage-domain {domain_id} {fingerprint}\nhttp: {{}}\n"

  def routes(self, hostname):
    return sorted(path.rsplit("/", 1)[1] for host, path in self.files if host == hostname and "/domain-" in path)


class Zone(Provider):
  """Cloudflare as the pass lists it: the records apply wrote, as proxied CNAMEs, plus `others` as they are, and the
  account's tunnels by name (`listed`); `listing_fails` names what can't be listed (`zone`, `tunnels`)."""

  def __init__(self):
    super().__init__()
    del self.tunnels
    self.listed, self.others, self.listing_fails = {}, [], ()

  def find_tunnel(self, name):
    return self.listed.get(name)

  def get_dns_records(self, name=None):
    if "zone" in self.listing_fails:
      raise Exception("zone listing failed")
    return [{"id": f"{comment}/{host}", "comment": comment, "name": host, "content": content, "type": "CNAME", "proxied": True}
            for comment, names in self.zone.items() for host, content in names.items()] + [dict(record) for record in self.others]

  def tunnels(self):
    if "tunnels" in self.listing_fails:
      raise Exception("tunnel listing failed")
    return dict(self.listed)

  def sync_dns_records(self, records, zone=None, **fields):
    super().sync_dns_records(records, zone, **fields)
    self.others = [record for record in self.others if record["comment"] not in records]


@pytest.fixture
def hosts(register):
  return register(Tailscale, ListedHosts())


@pytest.fixture
def provider(register):
  provider = register(Cloudflare, Zone())
  provider.listed = {"sage-example.test-w1": {"id": "t1"}, "sage-example.test-w2": {"id": "t2"}}
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


def conforming(hosts, provider, domain):
  """Its route and DNS as its apply would leave them, as a restarted Manager finds them."""
  snapshot = DomainConfig.capture(Domain.get_by_id(domain.id), "example.test")
  hosts.files[("w1", f"{DYNAMIC}/{snapshot.filename}")] = snapshot.route()
  provider.zone[snapshot.dns_comment] = snapshot.dns_records({"id": "t1"})


def applies(manager):
  return sorted(params["domain_id"] for key, _, params, _, _ in queued(manager) if key[0] == "apply_domain")


def writes(hosts):
  return [command for command in hosts.commands if command[1] != "list"]


class TestRequest:
  def test_the_pass_holds_every_domain_quietly(self, manager):
    manager.request_domain_reconciliation()
    assert queued(manager) == [(("reconcile_domains",), ["domain"], {}, {}, True)]

  async def test_a_request_while_one_runs_gets_a_pass_after_it(self, manager, public, hosts):
    seen = []

    def again():
      manager.request_domain_reconciliation()
      manager.task_queue.dispatch_tick()
      seen.append(tuple(len(tasks) for tasks in manager.task_queue.snapshot().values()))

    hosts.while_listing = again
    manager.request_domain_reconciliation()
    await manager.run()
    assert seen == [(1, 1)]
    assert [command for command in hosts.commands if command == ("w1", "list")] == [("w1", "list")] * 2

  async def test_an_apply_requested_while_it_runs_waits_for_it(self, manager, public, hosts):
    seen = []

    def apply():
      manager.request_domain_applies([public.id])
      manager.task_queue.dispatch_tick()
      seen.append([task["name"] for task in manager.task_queue.snapshot()["running"]])

    hosts.while_listing = apply
    manager.request_domain_reconciliation()
    await manager.run()
    assert seen == [["reconcile_domains"]]
    assert hosts.route("w1", public) == fingerprint(public)


class TestObservation:
  async def test_after_a_restart_conforming_artifacts_show_it_synced_and_nothing_is_applied(
          self, manager, public, internal, hosts, provider):
    for domain in (public, internal):
      conforming(hosts, provider, domain)
    await manager.reconcile_domains()
    assert {domain.id: state(manager, domain).view.status for domain in (public, internal)} == {
        public.id: "synced", internal.id: "synced"}
    assert (applies(manager), writes(hosts), provider.calls) == ([], [], [])
    assert Domains().route(public.id, "w1") == RouteObservation("present", fingerprint(public))
    assert Domains().dns_conforms(public.id, fingerprint(public)) is True

  @pytest.mark.parametrize("route", ["missing", "older", "not its own"])
  async def test_a_route_that_is_not_the_current_one_is_applied_again(self, manager, public, hosts, provider, route):
    conforming(hosts, provider, public)
    if route == "missing":
      hosts.files.clear()
    elif route == "older":
      hosts.put("w1", public.id, "0" * 64)
    else:
      hosts.files[("w1", f"{DYNAMIC}/domain-{public.id}.yml")] = f"# sage-domain {GONE} {fingerprint(public)}\n"
    await manager.reconcile_domains()
    seen = {"missing": RouteObservation("absent"), "older": RouteObservation("present", "0" * 64),
            "not its own": RouteObservation("present", None)}[route]
    assert (Domains().route(public.id, "w1"), applies(manager)) == (seen, [public.id])
    await manager.run()
    assert hosts.route("w1", public) == fingerprint(public) and state(manager, public).view.status == "synced"

  async def test_a_stopped_containers_route_is_seen_and_its_apply_removes_it(self, manager, public, hosts, provider):
    conforming(hosts, provider, public)
    manager.routable[public.container_id] = False
    await manager.reconcile_domains()
    assert hosts.route("w1", public) == fingerprint(public) and applies(manager) == [public.id]
    await manager.run()
    assert hosts.route("w1", public) is None and state(manager, public).view.status == "synced"

  async def test_while_its_container_is_not_known_its_route_is_neither_judged_nor_applied(self, manager, public, hosts, provider):
    conforming(hosts, provider, public)
    hosts.put("w1", public.id, "0" * 64)
    manager.routable[public.container_id] = None
    await manager.reconcile_domains()
    assert (state(manager, public).view.reason, applies(manager), writes(hosts)) == ("eligibility_unknown", [], [])

  @pytest.mark.parametrize("record", [
      {"name": "shop.example.test", "content": "t2.cfargotunnel.com", "type": "CNAME", "proxied": True},
      {"name": "shop.example.test", "content": "t1.cfargotunnel.com", "type": "CNAME", "proxied": False},
      {"name": "shop.example.test", "content": "100.64.0.2", "type": "A", "proxied": False},
      {"name": "old.example.test", "content": "t1.cfargotunnel.com", "type": "CNAME", "proxied": True},
      None])
  async def test_dns_records_that_are_not_what_its_configuration_gives_are_applied_again(
          self, manager, public, hosts, provider, record):
    conforming(hosts, provider, public)
    comment = f"sage-domain-{public.id}"
    provider.zone.pop(comment)
    if record is not None:
      provider.others.append({"id": "r1", "comment": comment, **record})
    await manager.reconcile_domains()
    assert Domains().dns_conforms(public.id, fingerprint(public)) is False and applies(manager) == [public.id]
    await manager.run()
    assert provider.get_dns_records() == [{"id": f"{comment}/shop.example.test", "comment": comment, "name": "shop.example.test",
                                           "content": "t1.cfargotunnel.com", "type": "CNAME", "proxied": True}]

  async def test_a_public_name_whose_worker_has_no_tunnel_does_not_conform(self, manager, public, hosts, provider):
    conforming(hosts, provider, public)
    provider.listed = {}
    await manager.reconcile_domains()
    assert Domains().dns_conforms(public.id, fingerprint(public)) is False and applies(manager) == [public.id]

  async def test_an_unlinked_public_domain_is_applied_once_since_no_route_of_it_can_be_seen(self, manager, public, hosts, provider):
    Domain.update(container=None).where(Domain.id == public.id).execute()
    provider.zone[f"sage-domain-{public.id}"] = {}
    await manager.reconcile_domains()
    assert applies(manager) == [public.id]
    await manager.run()
    await manager.reconcile_domains()
    assert (state(manager, public).view.status, applies(manager)) == ("synced", [])

  async def test_a_failed_apply_is_tried_again_by_the_pass(self, manager, public, hosts, provider):
    conforming(hosts, provider, public)
    hosts.found["w1"] = "unreachable"
    Domains().record_outcome(public, ApplyOutcome(fingerprint(public), False, "dns", "rate limited"))
    assert state(manager, public).view.status == "error"
    await manager.reconcile_domains()
    assert applies(manager) == [public.id]

  async def test_an_apply_queued_already_is_not_requested_again(self, manager, public, hosts, provider):
    manager.request_domain_applies([public.id])
    [before] = manager.task_queue.snapshot()["queued"]
    await manager.reconcile_domains()
    assert [task["task_id"] for task in manager.task_queue.snapshot()["queued"]] == [before["task_id"]]

  @pytest.mark.parametrize("found, setup", [("unreachable", "succeeded"), ("unknown", "succeeded"), ("reachable", None),
                                            ("reachable", "refused"), ("reachable", "failed")])
  async def test_a_worker_not_reachable_and_set_up_is_not_looked_at(self, manager, public, hosts, provider, found, setup):
    conforming(hosts, provider, public)
    hosts.put("w1", GONE, "0" * 64)
    Domains().record_route(public, "w1", RouteObservation("present", fingerprint(public)))
    hosts.found["w1"] = found
    Workers().record_setup("w1", setup)
    await manager.reconcile_domains()
    assert [command for command in hosts.commands if command[0] == "w1"] == []
    assert hosts.routes("w1") == sorted([f"domain-{public.id}.yml", f"domain-{GONE}.yml"])
    assert Domains().route(public.id, "w1") == RouteObservation("unreachable")

  async def test_a_failed_listing_fails_the_pass_after_the_rest_of_it(self, manager, public, hosts, provider, application, notices):
    moved = Domain.create(name="moved", type="public", port=80,
                          container=Container.create(application=application, worker="w2"))
    hosts.put("w2", GONE, "0" * 64)
    hosts.listing_fails = "w1"
    with pytest.raises(TaskFailed):
      await manager.reconcile_domains()
    assert Domains().route(public.id, "w1") == RouteObservation("unreachable")
    [(level, content, link)] = notices()
    assert (level, content.startswith("The domains could not all be checked: the routing files on w1 could not be listed"), link) == (
        "error", True, f"{MANAGER}/domains")
    assert (hosts.routes("w2"), Domains().route(moved.id, "w2")) == ([], RouteObservation("absent"))
    assert applies(manager) == sorted([public.id, moved.id])

  @pytest.mark.parametrize("failing", ["zone", "tunnels"])
  async def test_without_the_zone_or_the_tunnels_dns_is_not_judged_and_the_pass_fails(
          self, manager, public, hosts, provider, failing):
    conforming(hosts, provider, public)
    Domains().record_dns(public, fingerprint(public), True)
    provider.zone[f"sage-domain-{public.id}"] = {}
    provider.listing_fails = (failing,)
    with pytest.raises(TaskFailed):
      await manager.reconcile_domains()
    assert Domains().dns_conforms(public.id, fingerprint(public)) is True
    assert (state(manager, public).view.status, applies(manager)) == ("synced", [])

  async def test_each_pass_republishes_discovery_failed_ones_included(self, manager, public, hosts, provider):
    await manager.reconcile_domains()
    hosts.listing_fails = "w1"
    with pytest.raises(TaskFailed):
      await manager.reconcile_domains()
    assert manager.discovered == 2

  async def test_the_facts_held_of_domains_gone_are_dropped(self, manager, public, hosts, provider):
    gone = Domain.create(name="gone", type="public", port=80, container=public.container)
    for domain in (public, gone):
      Domains().record_outcome(domain, ApplyOutcome("f1", True))
    Domain.delete().where(Domain.id == gone.id).execute()
    await manager.reconcile_domains()
    assert (Domains().outcome(gone.id), Domains().outcome(public.id)) == (None, ApplyOutcome("f1", True))


class TestCleanup:
  async def test_routes_of_domains_gone_or_bound_to_another_worker_are_removed_in_one_command(
          self, manager, public, hosts, provider, application):
    moved = Domain.create(name="moved", type="public", port=80,
                          container=Container.create(application=application, worker="w2"))
    for hostname, domain_id in (("w1", public.id), ("w1", moved.id), ("w1", GONE), ("w2", moved.id)):
      hosts.put(hostname, domain_id, "0" * 64)
    await manager.reconcile_domains()
    assert (hosts.routes("w1"), hosts.routes("w2")) == ([f"domain-{public.id}.yml"], [f"domain-{moved.id}.yml"])
    assert writes(hosts) == [("w1", "rm -f " + " ".join(f"{DYNAMIC}/domain-{domain_id}.yml" for domain_id in sorted((moved.id, GONE))))]

  async def test_an_unlinked_public_domains_route_goes_and_an_unlinked_internal_one_stays_for_its_apply(
          self, manager, public, internal, hosts, provider):
    Domain.update(container=None).where(Domain.id.in_([public.id, internal.id])).execute()
    for domain in (public, internal):
      hosts.put("w1", domain.id, "0" * 64)
    await manager.reconcile_domains()
    assert hosts.routes("w1") == [f"domain-{internal.id}.yml"]
    assert applies(manager) == sorted([public.id, internal.id])

  async def test_a_route_is_never_removed_for_its_containers_state(self, manager, public, hosts, provider):
    conforming(hosts, provider, public)
    manager.routable[public.container_id] = False
    await manager.reconcile_domains()
    assert writes(hosts) == [] and hosts.routes("w1") == [f"domain-{public.id}.yml"]

  async def test_a_domain_created_while_the_workers_are_listed_is_not_taken_for_one_gone(self, manager, public, hosts, provider):
    created = []

    def create():
      created.append(Domain.create(name="new", type="public", port=80, container=public.container))
      hosts.put("w1", created[0].id, "0" * 64)

    hosts.while_listing = create
    await manager.reconcile_domains()
    assert f"domain-{created[0].id}.yml" in hosts.routes("w1")

  async def test_a_removal_that_fails_on_one_worker_fails_the_pass_and_the_others_go_on(self, manager, public, hosts, provider):
    for hostname in ("w1", "w2"):
      hosts.put(hostname, GONE, "0" * 64)
      hosts.put(hostname, "fedcba9876543210", "0" * 64)
    original = hosts.exec_command

    async def exec_command(hostname, command, timeout=300):
      if hostname == "w1" and command.startswith("rm -f "):
        raise Exception("Error executing command on w1: exit code 1")
      return await original(hostname, command, timeout)

    hosts.exec_command = exec_command
    with pytest.raises(TaskFailed):
      await manager.reconcile_domains()
    assert (len(hosts.routes("w1")), hosts.routes("w2")) == (2, [])

  async def test_the_dns_records_of_domains_gone_are_removed_and_no_others(self, manager, public, hosts, provider):
    conforming(hosts, provider, public)
    provider.zone[f"sage-domain-{GONE}"] = {"gone.example.test": "t1.cfargotunnel.com"}
    provider.others = [{"id": "w", "comment": "sage-worker-w1", "name": "*.red.int.example.test", "content": "100.64.0.2",
                        "type": "A", "proxied": False},
                       {"id": "u", "comment": None, "name": "www.example.test", "content": "example.org", "type": "CNAME",
                        "proxied": True}]
    await manager.reconcile_domains()
    assert provider.calls == [({f"sage-domain-{GONE}": {}}, {})]
    assert (provider.zone[f"sage-domain-{GONE}"], len(provider.others)) == ({}, 2)

  async def test_a_failed_dns_removal_fails_the_pass(self, manager, public, hosts, provider):
    provider.zone[f"sage-domain-{GONE}"] = {"gone.example.test": "t1.cfargotunnel.com"}
    provider.fails = "rate limited"
    with pytest.raises(TaskFailed):
      await manager.reconcile_domains()
