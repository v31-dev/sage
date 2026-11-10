import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.db.models import Container, Domain, Instance, Worker, WorkerEvent
from services.manager.domains import DomainsMixin
from services.manager.queue import SCOPES, QueueMixin
from services.manager.workers import WorkersMixin
from services.tailscale import Tailscale
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from services.workers.rendering import InfraInputs, infra_fingerprint
from services.workers.state import Workers
from utils.executor import active_executor
from utils.logging import TaskFailed
from utils.queue import TaskQueue

HOME = "/opt/sage"


class Hosts:
  """The tailnet and the workers on it as convergence sees them: each worker's SSH fact and address, its files by path,
  the commands run on it, and a command that fails (`fails`, a part of it)."""

  def __init__(self):
    self.found, self.addresses, self.files, self.fails, self.starts = {}, {}, {}, None, False
    self.answering, self.boots, self.commands, self.forgotten, self.on_boot = set(), [], [], [], None

  async def observe_workers(self, hostnames):
    pass

  async def observe_worker(self, hostname):
    if hostname in self.answering:
      self.found[hostname] = "reachable"

  async def reachable(self, hostname):
    return hostname in self.answering

  async def forget(self, hostname):
    self.forgotten.append(hostname)

  def ssh(self, hostname):
    return self.found.get(hostname, "unobserved")

  def address(self, hostname):
    return self.addresses.get(hostname)

  def path(self, address):
    return None

  def ip(self):
    return "100.64.0.1"

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append(command)
    if self.fails and self.fails in command:
      raise Exception(f"Error executing command on {hostname}: exit code 1")
    if command == "cat /proc/sys/kernel/random/boot_id":
      boot = self.boots.pop(0) if len(self.boots) > 1 else self.boots[0]
      if self.on_boot:
        self.on_boot(boot)
      return 0, [boot]
    if command.startswith("grep -H . "):
      prefix = f"{hostname}:{HOME}/revisions/"
      return 0, [f"{path[len(hostname) + 1:]}:{content}" for path, (content, _) in self.files.items() if path.startswith(prefix)]
    if command.startswith("stat -c %g"):
      return 0, ["998"]
    if command.startswith("grep -H -m1 "):
      prefix = f"{hostname}:{HOME}/traefik/dynamic/domain-"
      return 0, [f"{path[len(hostname) + 1:]}:{content.splitlines()[0]}" for path, (content, _) in self.files.items()
                 if path.startswith(prefix)]
    if command.startswith("rm -f "):
      for path in command[len("rm -f "):].split():
        self.files.pop(f"{hostname}:{path}", None)
    if " up -d --quiet-pull " in command:
      return 0, [" Container sage-traefik-1  Started"] if self.starts else []
    return 0, []

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    self.files[f"{hostname}:{dst}"] = (keys["CONTENT"], mode)

  def stack(self, hostname):
    """The stack's files on the worker, by path under its home."""
    return {path.split(f"{HOME}/", 1)[1]: file for path, file in self.files.items()
            if path.startswith(f"{hostname}:") and "/revisions/" not in path}

  def stamp(self, hostname, name):
    file = self.files.get(f"{hostname}:{HOME}/revisions/{name}")
    return file[0] if file else None


class Provider:
  """Cloudflare as convergence uses it: the tunnels made and the worker records synced."""

  def __init__(self):
    self.domain, self.tunnel_id, self.records, self.tunnels = "example.test", "tunnel-platform", {}, []

  def connect(self):
    pass

  def worker_tunnel_name(self, hostname):
    return f"sage-{self.domain}-{hostname}"

  def create_tunnel(self, name):
    self.tunnels.append(name)
    return f"tunnel-{name}"

  def sync_worker_records(self, hostname, subdomain=None, ip=None):
    self.records[hostname] = (subdomain, ip)

  def get_tunnel_token(self, tunnel_id=None):
    return SimpleNamespace(token=f"token-{tunnel_id or self.tunnel_id}")

  def delete_tunnel(self, name):
    if getattr(self, "keeps_tunnels", False):
      raise Exception("the tunnel has connections")
    self.tunnels.remove(name)


class Catalog:
  """The certificates as worker setup sees them: whether each of the worker's covers its names (`covered`, by role),
  and whether the worker applied the current one, which is the proof `current` names for the role."""

  def __init__(self):
    self.covered = {"worker": True, "discovery": True}
    self.current = {"worker": "p1", "discovery": "p2"}

  def worker_state(self, hostname, subdomain, applied, template):
    return {role: {"covered": covered, "synced": covered and (applied or {}).get(role) == self.current[role]}
            for role, covered in self.covered.items()}


class Manager(WorkersMixin, DomainsMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = self.workers_queue_facts()
    self.worker_state = self.workers_state_fact()
    self.setup_failures, self.stack_repairs = {}, {}
    self.applied, self.reconciled, self.discovered, self.renewals, self.distributions = [], 0, 0, 0, []
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  def request_domain_applies(self, domain_ids):
    if ids := sorted(domain_ids):
      self.applied.append(ids)

  def request_domain_reconciliation(self):
    self.reconciled += 1

  def request_discovery(self):
    self.discovered += 1

  def request_certificate_renewal(self):
    self.renewals += 1

  def request_certificate_distribution(self, hostname, force=False):
    self.distributions.append(hostname)


@pytest.fixture
def hosts(register, clock):
  return register(Tailscale, Hosts())


@pytest.fixture
def provider(register):
  return register(Cloudflare, Provider())


@pytest.fixture
def catalog(register):
  return register(Certs, Catalog())


@pytest.fixture
def manager(hosts, provider, catalog, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", HOME)
  manager = Manager()
  # The task bodies run here as the queue runs them, with their lane bound for what they offload.
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def worker(db, hosts):
  hosts.found["w1"], hosts.addresses["w1"] = "reachable", "100.64.0.2"
  return Worker.create(hostname="w1", subdomain="red")


@pytest.fixture
def monotonic(monkeypatch):
  import services.manager.workers as module
  clock = SimpleNamespace(now=1000.0)
  monkeypatch.setattr(module, "monotonic", lambda: clock.now)
  return clock


def fingerprint(address="100.64.0.2"):
  return infra_fingerprint(InfraInputs("w1", address, "100.64.0.1", "example.test", "tunnel-platform",
                                       "tunnel-sage-example.test-w1", config.VERSION, config.REPORT_INTERVAL_SECONDS))


def events():
  return [(event.action, event.transition) for event in WorkerEvent.select().order_by(WorkerEvent.id)]


def requested(manager):
  return [(task["name"], task["key"], task["scopes"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]]


def report(hostname="w1", running=config.WORKER_STACK_SERVICES, at=datetime(2026, 1, 1, 9)):
  readings = {name: ContainerReading(name=name, status="running", image="", labels={}, exit_code=0, oom_killed=False, started_at=1,
                                     finished_at=None, health=None, cpu_percent=None, mem_bytes=None, net_rx_bytes=None,
                                     net_tx_bytes=None) for name in running}
  WorkerReports().accept(hostname, [Snapshot(at=at, host=HostReading(*([None] * 13)), containers=readings,
                                             traefik_reachable=True)])


class TestConvergence:
  async def test_a_new_worker_is_claimed_given_its_tunnel_and_record_and_its_stack_is_applied(
          self, manager, hosts, provider, worker):
    await manager.converge_worker("w1")
    assert hosts.stamp("w1", "owner") == f"{Instance.get().id} example.test"
    assert (provider.tunnels, provider.records) == (["sage-example.test-w1"], {"w1": ("red", "100.64.0.2")})
    stack = hosts.stack("w1")
    assert set(stack) == {"docker-compose.yml", ".env", "traefik/traefik.yml", "traefik/dynamic/config.yml",
                          "vector/config/vector.yml", "telegraf/telegraf.conf"}
    assert stack[".env"][1] == 0o600 and "TUNNEL_TOKEN=token-tunnel-sage-example.test-w1" in stack[".env"][0]
    assert "DOCKER_GID=998" in stack[".env"][0]
    assert hosts.stamp("w1", "infra") == fingerprint()
    assert (Workers().setup("w1"), Workers().setup_address("w1")) == ("succeeded", "100.64.0.2")
    assert events() == [("infrastructure", "admitted"), ("infrastructure", "succeeded")]

  async def test_a_worker_whose_stamp_matches_only_gets_its_stack_started(self, manager, hosts, worker):
    await manager.converge_worker("w1")
    hosts.files = {path: file for path, file in hosts.files.items() if "/revisions/" in path}
    await manager.converge_worker("w1")
    assert hosts.stack("w1") == {} and Workers().setup("w1") == "succeeded"
    assert events() == [("infrastructure", "admitted"), ("infrastructure", "succeeded")]

  async def test_force_applies_whatever_the_stamp_says(self, manager, hosts, worker):
    await manager.converge_worker("w1")
    hosts.files = {path: file for path, file in hosts.files.items() if "/revisions/" in path}
    await manager.converge_worker("w1", force=True)
    assert len(hosts.stack("w1")) == 6
    assert [transition for _, transition in events()] == ["admitted", "succeeded"] * 2

  async def test_a_new_address_applies_again(self, manager, hosts, worker):
    await manager.converge_worker("w1")
    hosts.addresses["w1"] = "100.64.0.9"
    await manager.converge_worker("w1")
    assert hosts.stamp("w1", "infra") == fingerprint("100.64.0.9") and "TS_IP=100.64.0.9" in hosts.stack("w1")[".env"][0]

  async def test_another_sages_worker_is_refused_and_left_as_it_is(self, manager, hosts, provider, worker):
    hosts.files[f"w1:{HOME}/revisions/owner"] = ("ffffffffffffffff other.test", 0o644)
    await manager.converge_worker("w1")
    await manager.converge_worker("w1")
    assert (hosts.stack("w1"), provider.tunnels, hosts.stamp("w1", "owner")) == ({}, [], "ffffffffffffffff other.test")
    assert Workers().setup("w1") == "refused" and "w1" in manager.setup_failures
    assert events() == [("ownership", "rejected")]

  async def test_a_setup_that_fails_records_it_and_writes_no_stamp(self, manager, hosts, worker):
    hosts.fails = "up -d --wait"
    with pytest.raises(TaskFailed):
      await manager.converge_worker("w1")
    assert (Workers().setup("w1"), hosts.stamp("w1", "infra")) == ("failed", None)
    assert "w1" in manager.setup_failures
    assert events() == [("infrastructure", "admitted"), ("infrastructure", "failed")]

  async def test_a_success_clears_the_failure(self, manager, hosts, worker):
    manager.setup_failures["w1"] = 1.0
    await manager.converge_worker("w1")
    assert "w1" not in manager.setup_failures

  async def test_a_worker_that_does_not_answer_is_not_set_up(self, manager, hosts, worker):
    hosts.found["w1"] = "unreachable"
    await manager.converge_worker("w1")
    assert hosts.files == {} and Workers().setup("w1") is None
    assert manager.reconciled == 0

  async def test_once_it_is_set_up_the_pass_over_the_domains_is_asked_for(self, manager, hosts, worker):
    await manager.converge_worker("w1")
    assert manager.reconciled == 1

  async def test_a_worker_that_applied_the_current_certificates_asks_for_neither_renewal_nor_copy(self, manager, hosts, worker):
    hosts.files[f"w1:{HOME}/revisions/certs"] = ('{"discovery": "p2", "worker": "p1"}', 0o644)
    await manager.converge_worker("w1")
    assert Workers().certificates("w1") == {"discovery": "p2", "worker": "p1"}
    assert (manager.renewals, manager.distributions) == (0, [])

  @pytest.mark.parametrize("stamp", [None, '{"discovery": "p2", "worker": "old"}', "not json"])
  async def test_a_worker_that_did_not_apply_them_gets_its_copy(self, manager, hosts, worker, stamp):
    if stamp is not None:
      hosts.files[f"w1:{HOME}/revisions/certs"] = (stamp, 0o644)
    await manager.converge_worker("w1")
    assert (manager.renewals, manager.distributions) == (0, ["w1"])

  @pytest.mark.parametrize("role", ["worker", "discovery"])
  async def test_a_certificate_that_does_not_cover_its_names_is_renewed(self, manager, hosts, worker, catalog, role):
    catalog.covered[role] = False
    await manager.converge_worker("w1")
    assert (manager.renewals, manager.distributions) == (1, [])

  async def test_a_setup_that_fails_or_is_refused_asks_for_no_pass(self, manager, hosts, worker):
    hosts.fails = "up -d --wait"
    with pytest.raises(TaskFailed):
      await manager.converge_worker("w1")
    hosts.fails, hosts.files = None, {f"w1:{HOME}/revisions/owner": ("ffffffffffffffff other.test", 0o644)}
    await manager.converge_worker("w1")
    assert manager.reconciled == 0


class TestRoutesLeftBehind:
  @pytest.fixture
  def routed(self, hosts, worker, application):
    """A Domain bound to w1 and one moved to w2, each with a routing file on w1, and the file of a Domain gone."""
    other = Worker.create(hostname="w2", subdomain="blue")
    here = Domain.create(name="shop", type="public", port=80, container=Container.create(application=application, worker=worker))
    moved = Domain.create(name="api", type="public", port=80, container=Container.create(application=application, worker=other))
    gone = "0123456789abcdef"
    for domain_id in (here.id, moved.id, gone):
      hosts.files[f"w1:{HOME}/traefik/dynamic/domain-{domain_id}.yml"] = (f"# sage-domain {domain_id} {'0' * 64}\n", 0o644)
    return here, moved, gone

  def left(self, hosts):
    return sorted(path.rsplit("/", 1)[1] for path in hosts.files if "/traefik/dynamic/domain-" in path)

  async def test_a_returning_worker_loses_the_routes_of_domains_gone_or_bound_elsewhere(self, manager, hosts, routed):
    here, _, _ = routed
    await manager.converge_worker("w1")
    assert self.left(hosts) == [f"domain-{here.id}.yml"]

  async def test_unlinked_public_domains_routes_go_and_an_unlinked_internal_one_stays_on_its_namespace_worker(
          self, manager, hosts, routed, worker):
    here, moved, _ = routed
    Domain.update(container=None).where(Domain.id == here.id).execute()
    internal = Domain.create(name="db", type="tcp", port=5432, namespace_worker=worker)
    hosts.files[f"w1:{HOME}/traefik/dynamic/domain-{internal.id}.yml"] = (f"# sage-domain {internal.id} {'0' * 64}\n", 0o644)
    await manager.converge_worker("w1")
    assert self.left(hosts) == [f"domain-{internal.id}.yml"]

  async def test_they_are_removed_before_the_stack_starts_in_one_command(self, manager, hosts, routed):
    _, moved, gone = routed
    await manager.converge_worker("w1")
    removal = next(index for index, command in enumerate(hosts.commands) if command.startswith("rm -f "))
    assert hosts.commands[removal] == "rm -f " + " ".join(
        f"{HOME}/traefik/dynamic/domain-{domain_id}.yml" for domain_id in sorted((moved.id, gone)))
    assert removal < next(index for index, command in enumerate(hosts.commands) if " up -d " in command)

  async def test_a_removal_that_fails_fails_the_setup(self, manager, hosts, routed):
    hosts.fails = "rm -f"
    with pytest.raises(TaskFailed):
      await manager.converge_worker("w1")
    assert Workers().setup("w1") == "failed" and len(self.left(hosts)) == 3

  async def test_another_sages_worker_keeps_its_routes(self, manager, hosts, routed):
    hosts.files[f"w1:{HOME}/revisions/owner"] = ("ffffffffffffffff other.test", 0o644)
    await manager.converge_worker("w1")
    assert len(self.left(hosts)) == 3


class TestStackRepair:
  @pytest.mark.parametrize("starts, recorded", [(True, [("repair", "succeeded")]), (False, [])])
  async def test_a_repair_is_recorded_when_it_started_something(self, manager, hosts, worker, starts, recorded):
    hosts.starts = starts
    await manager.repair_worker_stack("w1")
    assert events() == recorded


class TestRequests:
  async def test_a_reachable_worker_not_set_up_is_converged_quietly_and_one_that_does_not_answer_is_not(
          self, manager, hosts, worker, monotonic):
    Worker.create(hostname="w2", subdomain="blue")
    hosts.found["w2"], hosts.addresses["w2"] = "unreachable", "100.64.0.3"
    await manager.observe_workers()
    assert requested(manager) == [("converge_worker", ["converge_worker", "w1"], ["worker:w1"], True)]

  async def test_a_failed_setup_is_tried_again_only_after_the_retry_time(self, manager, hosts, worker, monotonic):
    Workers().record_setup("w1", "failed", "100.64.0.2")
    manager.setup_failures["w1"] = monotonic.now
    monotonic.now += config.WORKER_SETUP_RETRY_SECONDS - 1
    await manager.observe_workers()
    assert requested(manager) == []
    monotonic.now += 1
    await manager.observe_workers()
    assert [name for name, *_ in requested(manager)] == ["converge_worker"]

  async def test_a_worker_whose_address_changed_is_converged(self, manager, hosts, worker, monotonic):
    Workers().record_setup("w1", "succeeded", "100.64.0.2")
    report()
    hosts.addresses["w1"] = "100.64.0.9"
    await manager.observe_workers()
    assert [name for name, *_ in requested(manager)] == ["converge_worker"]

  async def test_a_set_up_worker_with_its_stack_down_gets_it_repaired_once_per_cooldown(self, manager, hosts, worker, monotonic):
    Workers().record_setup("w1", "succeeded", "100.64.0.2")
    report(running=config.WORKER_STACK_SERVICES[1:])
    await manager.observe_workers()
    assert requested(manager) == [("repair_worker_stack", ["repair_worker_stack", "w1"], ["worker:w1:stack"], True)]
    manager.task_queue.cancel(scope="worker")
    monotonic.now += config.WORKER_STACK_REPAIR_COOLDOWN_SECONDS - 1
    await manager.observe_workers()
    assert requested(manager) == []
    monotonic.now += 1
    await manager.observe_workers()
    assert [name for name, *_ in requested(manager)] == ["repair_worker_stack"]

  async def test_a_worker_silent_for_less_than_a_minute_is_not_repaired(self, manager, hosts, worker, monotonic, clock):
    Workers().record_setup("w1", "succeeded", "100.64.0.2")
    report()
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    await manager.observe_workers()
    assert requested(manager) == []
    clock.advance(timedelta(seconds=config.WORKER_STACK_SILENCE_SECONDS))
    await manager.observe_workers()
    assert [name for name, *_ in requested(manager)] == ["repair_worker_stack"]

  def test_a_forced_convergence_is_a_visible_task_of_its_own(self, manager, worker):
    manager.request_convergence("w1")
    manager.request_convergence("w1", force=True)
    assert [(key, quiet) for _, key, _, quiet in requested(manager)] == [
        (["converge_worker", "w1"], True), (["converge_worker", "w1", "force"], False)]


class TestAdd:
  def test_the_request_writes_the_row_and_queues_the_add(self, manager, db):
    manager.request_add_worker("w1", "red")
    assert Worker.get_by_id("w1").subdomain == "red"
    assert [(name, quiet) for name, _, _, quiet in requested(manager)] == [("add_worker", False)]

  def test_a_worker_already_added_is_refused(self, manager, worker):
    from utils.errors import Conflict
    with pytest.raises(Conflict):
      manager.request_add_worker("w1", "green")

  async def test_the_add_looks_at_the_worker_it_has_not_observed_yet_and_is_its_first_setup(self, manager, hosts, worker):
    hosts.found["w1"] = "unobserved"
    hosts.answering.add("w1")
    await manager.add_worker("w1")
    assert Workers().setup("w1") == "succeeded"
    assert events() == [("add", "admitted"), ("infrastructure", "admitted"), ("infrastructure", "succeeded"), ("add", "succeeded")]

  @pytest.mark.parametrize("trouble", ["setup fails", "does not answer"])
  async def test_a_worker_whose_first_setup_did_not_happen_is_kept(self, manager, hosts, worker, trouble):
    if trouble == "setup fails":
      hosts.answering.add("w1")
      hosts.fails = "up -d --wait"
    else:
      hosts.found["w1"] = "unobserved"
    with pytest.raises(TaskFailed):
      await manager.add_worker("w1")
    assert Worker.get_or_none(Worker.hostname == "w1") is not None
    assert events()[0] == ("add", "admitted") and events()[-1] == ("add", "failed")


class TestRemove:
  async def test_a_worker_that_answers_and_is_this_sages_is_cleaned_and_forgotten(self, manager, hosts, provider, worker):
    await manager.converge_worker("w1")
    hosts.answering.add("w1")
    manager.setup_failures["w1"] = 1.0
    report()
    await manager.remove_worker("w1")
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    assert any(command.endswith(f"down --remove-orphans && rm -rf {HOME}") for command in hosts.commands)
    assert (provider.records["w1"], provider.tunnels, hosts.forgotten) == ((None, None), [], ["w1"])
    assert (Workers().setup("w1"), WorkerReports().latest("w1"), manager.setup_failures) == (None, None, {})
    assert events()[-2:] == [("remove", "admitted"), ("remove", "succeeded")]

  async def test_a_removal_renews_the_certificates_so_its_leaf_is_retired_and_forgets_what_it_applied(
          self, manager, hosts, provider, worker):
    Workers().record_certificates("w1", {"worker": "p1"})
    await manager.remove_worker("w1")
    assert (manager.renewals, Workers().certificates("w1")) == (1, None)

  async def test_removing_a_worker_that_was_set_up_republishes_discovery_without_it(self, manager, hosts, provider, worker):
    await manager.converge_worker("w1")
    hosts.answering.add("w1")
    before = manager.discovered
    await manager.remove_worker("w1")
    assert manager.discovered == before + 1

  @pytest.mark.parametrize("trouble", ["another Sage's", "does not answer"])
  async def test_a_worker_it_cannot_clean_is_removed_from_sage_only(self, manager, hosts, provider, worker, trouble):
    if trouble == "another Sage's":
      hosts.answering.add("w1")
      hosts.files[f"w1:{HOME}/revisions/owner"] = ("ffffffffffffffff other.test", 0o644)
    await manager.remove_worker("w1")
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    assert not any("rm -rf" in command for command in hosts.commands)

  async def test_a_tunnel_cloudflare_will_not_delete_does_not_stop_the_removal(self, manager, hosts, provider, worker):
    await manager.converge_worker("w1")
    provider.keeps_tunnels = True
    await manager.remove_worker("w1")
    assert Worker.get_or_none(Worker.hostname == "w1") is None and events()[-1] == ("remove", "succeeded")

  async def test_a_worker_with_a_container_is_refused_and_untouched(self, manager, hosts, worker, application):
    from services.db.models import Container
    Container.create(application=application, worker=worker)
    from utils.errors import Conflict
    with pytest.raises(Conflict):
      manager.request_remove_worker("w1")
    with pytest.raises(TaskFailed):
      await manager.remove_worker("w1")
    assert Worker.get_or_none(Worker.hostname == "w1") is not None and events() == [("remove", "rejected")]


@pytest.fixture
def ticking(monkeypatch):
  """The reboot's clock moves 60 s at each look, and its waits are instant."""
  import services.manager.workers as module
  clock = SimpleNamespace(now=0.0)

  def tick():
    clock.now += 60
    return clock.now

  monkeypatch.setattr(module, "monotonic", tick)
  monkeypatch.setattr(config, "WORKER_REBOOT_POLL_SECONDS", 0)
  return clock


class TestReboot:
  @staticmethod
  def booting(clock, report_after=None):
    """What happens at each look at the boot id: time passes, and a report arrives once the worker runs `report_after`."""
    def look(boot):
      clock.advance(timedelta(seconds=10))
      if boot == report_after:
        report(at=datetime(2026, 1, 1, 10))
    return look

  async def test_a_new_boot_set_up_again_and_reporting_after_it_is_verified(self, manager, hosts, worker, ticking, clock):
    await manager.converge_worker("w1")
    hosts.answering.add("w1")
    report()
    hosts.boots = ["b1", "b1", "b2"]
    hosts.on_boot = self.booting(clock, report_after="b2")
    await manager.reboot_worker("w1")
    assert "(sleep 2 && reboot) </dev/null >/dev/null 2>&1 &" in hosts.commands
    assert Workers().setup("w1") == "succeeded"
    assert [event for event in events() if event[0] == "reboot"] == [("reboot", "admitted"), ("reboot", "verified")]

  @pytest.mark.parametrize("boots, outcome", [(["b1"], "old_boot"), (["b1", "b2"], "not_ready")],
                           ids=["it never rebooted", "no report after the new boot"])
  async def test_a_reboot_not_verified_in_time_fails_with_how_far_it_got(self, manager, hosts, worker, ticking, clock, boots,
                                                                         outcome):
    await manager.converge_worker("w1")
    hosts.answering.add("w1")
    report()
    hosts.boots, hosts.on_boot = boots, self.booting(clock)
    with pytest.raises(TaskFailed):
      await manager.reboot_worker("w1")
    reboot = WorkerEvent.select().where(WorkerEvent.action == "reboot").order_by(WorkerEvent.id.desc()).first()
    assert (reboot.transition, reboot.reason) == ("failed", {"outcome": outcome})

  async def test_a_worker_not_set_up_is_not_rebooted(self, manager, hosts, worker):
    with pytest.raises(TaskFailed):
      await manager.reboot_worker("w1")
    assert "(sleep 2 && reboot) </dev/null >/dev/null 2>&1 &" not in hosts.commands
    assert events() == [("reboot", "rejected")]


class TestSubdomain:
  def test_a_new_subdomain_converges_the_worker_so_its_record_moves(self, manager, worker):
    manager.worker_subdomain_changed("w1")
    assert [name for name, *_ in requested(manager)] == ["converge_worker"]

  def test_its_certificate_is_renewed_for_the_new_names_whether_or_not_it_answers(self, manager, worker):
    manager.worker_subdomain_changed("w1")
    assert manager.renewals == 1

  def test_the_internal_and_tcp_domains_under_it_are_applied_again(self, manager, worker, application):
    on_it = Container.create(application=application, worker="w1")
    under = sorted(Domain.create(name=name, type=type, port=80, container=container, namespace_worker="w1").id
                   for name, type, container in (("api", "internal", on_it), ("db", "tcp", on_it), ("old", "internal", None)))
    Domain.create(name="shop", type="public", port=80, container=on_it)
    Worker.create(hostname="w2", subdomain="blue")
    Domain.create(name="api", type="internal", port=80, namespace_worker="w2")
    manager.worker_subdomain_changed("w1")
    assert manager.applied == [under]
