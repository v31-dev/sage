import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services.applications.deploy_input import capture_deploy_config
from services.containers.placement import placement_dir, record_path, runtime_labels, runtime_name
from services.containers.record import Applied, Authority, Operation, Settled, TargetRead, TargetRecord, encode_record
from services.containers.targets import Containers
from services.db.models import Application, Container, Domain, Instance, Worker
from services.manager.containers import ContainersMixin
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from services.updates import UpdateSource
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from utils.queue import TaskQueue

WORKER_CLOCK = datetime(2026, 1, 1, 9)


class WorkerHosts:
  """The workers: commands run in a local shell and synced files land in each worker's own directory under the test's,
  as SSH and SFTP would put them on that worker; a worker in `unreachable` cannot be asked."""

  def __init__(self, root: Path):
    self.root = root
    self.unreachable = set()
    self.commands = []
    self.observed = {}

  def ssh(self, hostname):
    """What the workers' observation found of the worker's SSH server."""
    return self.observed.get(hostname, "unobserved")

  def local(self, hostname, text):
    """`text` with the worker's home replaced by that worker's own directory here."""
    return text.replace(config.WORKER_HOME_DIR, str(self.root / hostname))

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append((hostname, command))
    if hostname in self.unreachable:
      raise Exception(f"Error connecting to {hostname}: no route")
    result = subprocess.run(["sh", "-c", self.local(hostname, command)], capture_output=True, text=True, check=True)
    return result.returncode, result.stdout.splitlines()

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    if hostname in self.unreachable:
      raise Exception(f"Error connecting to {hostname}: no route")
    content = Path(src).read_text()
    for key, value in (keys or {}).items():
      content = content.replace("${" + key + "}", str(value))
    path = Path(self.local(hostname, dst))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class Manager(ContainersMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = self.containers_queue_facts()
    self.container_state = self.containers_state_fact()
    self.recoveries = {}
    self.orphans = {}
    self.applied = []
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  def request_domain_applies(self, domain_ids):
    if ids := sorted(domain_ids):
      self.applied.append(ids)

  async def run(self):
    self.task_queue.dispatch_tick()
    while self.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
      self.task_queue.dispatch_tick()


@pytest.fixture
def hosts(register, tmp_path, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", "/opt/sage-under-test")
  return register(Tailscale, WorkerHosts(tmp_path))


@pytest.fixture
def manager(hosts, clock):
  manager = Manager()
  yield manager
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def containers(application, worker):
  second = Worker.create(hostname="w2", subdomain="blue")
  return [Container.create(application=application, worker=host) for host in (worker, second)]


def authority(container):
  instance = Instance.get()
  return Authority(instance.id, instance.state_generation, container.id)


async def deployed(container):
  """The container's record of a deploy of its application's current inputs, as its Deploy writes it."""
  at = datetime(2026, 1, 1, 11, 59)
  config_hash = capture_deploy_config(container.application_id).content_hash
  await Containers().write(container, TargetRecord(
      authority(container), Applied("d1", config_hash, at), Settled("d1", "running", "succeeded", at),
      Operation("d1", "deploy", "settled", at, 905, at, config_hash, outcome="succeeded", verified_on_worker_at=WORKER_CLOCK)))


def report(container, state="running", deployment="d1", hostname=None, at=None):
  """A report from the container's worker, gathered after the deploy was verified, listing its runtime."""
  reading = ContainerReading(
      name=runtime_name(container.id), status=state, image="nginx",
      labels=runtime_labels(authority(container), deployment, "demo-web"), exit_code=0, oom_killed=False, started_at=1,
      finished_at=None, health=None, cpu_percent=None, mem_bytes=None, net_rx_bytes=None, net_tx_bytes=None)
  snapshot = Snapshot(at=at or WORKER_CLOCK + timedelta(seconds=10), host=HostReading(*([None] * 13)),
                      containers={reading.name: reading}, traefik_reachable=True)
  return WorkerReports().accept(hostname or container.worker_id, [snapshot])


def view(manager, container):
  return manager.container_states([container])[container.id].view


def pages(*containers):
  return [signal for container in containers for signal in (
      (UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, container.worker_id), (UpdateSource.DOMAINS, None))]


class TestStates:
  def test_a_container_whose_record_was_never_read_is_checking(self, manager, containers):
    assert view(manager, containers[0]).headline == "checking"

  async def test_a_container_running_the_deployment_its_record_committed_is_active_and_current(self, manager, containers):
    await deployed(containers[0])
    report(containers[0])
    state = manager.container_states([containers[0]])[containers[0].id]
    assert (state.view.headline, state.view.config, state.routable) == ("active", "current", True)

  async def test_an_edit_of_its_inputs_makes_it_dirty_and_leaves_it_active(self, manager, containers, application):
    await deployed(containers[0])
    report(containers[0])
    Application.update(env="GREETING=hi").where(Application.id == application.id).execute()
    assert (view(manager, containers[0]).headline, view(manager, containers[0]).config) == ("active", "dirty")

  async def test_a_record_from_before_a_restore_is_an_error_of_its_own(self, manager, containers):
    await deployed(containers[0])
    report(containers[0])
    Instance.update(state_generation="9999999999999999").execute()
    assert (view(manager, containers[0]).headline, view(manager, containers[0]).reason) == ("error", "restored")

  async def test_a_worker_whose_ssh_server_stopped_answering_makes_its_container_an_error(self, manager, hosts, containers):
    await deployed(containers[0])
    report(containers[0])
    hosts.observed["w1"] = "unreachable"
    assert (view(manager, containers[0]).headline, view(manager, containers[0]).reason) == ("error", "unreachable")
    hosts.observed["w1"] = "reachable"
    assert view(manager, containers[0]).headline == "active"

  async def test_a_report_from_another_worker_says_nothing_of_it(self, manager, containers):
    await deployed(containers[0])
    report(containers[0], hostname="w2", at=WORKER_CLOCK + timedelta(hours=1))
    report(containers[0], state="exited")
    assert view(manager, containers[0]).reason == "unexpected_exit"

  async def test_the_operation_running_on_it_is_its_progress_and_no_other_containers(self, manager, containers):
    release = asyncio.Event()

    async def holds(container_id, deploy):
      await release.wait()

    manager.deploy_container = holds
    manager.request_deploy(containers[0].id)
    manager.task_queue.dispatch_tick()
    states = manager.container_states(containers)
    assert (states[containers[0].id].view.headline, states[containers[0].id].live) == ("deploying", "deploy")
    assert states[containers[1].id].live is None
    release.set()
    await manager.run()
    assert manager.container_states(containers)[containers[0].id].live is None


class TestPublication:
  def test_a_report_change_shows_its_containers_again_and_no_others(self, manager, containers, signals):
    manager.containers_reported("w1", {runtime_name(containers[0].id), runtime_name(containers[1].id), "traefik",
                                       "sage-ffffffffffffffff"})
    assert signals == pages(containers[0])

  async def test_an_operation_starting_and_ending_shows_its_container_again(self, manager, containers, signals):
    release = asyncio.Event()

    async def holds(container_id):
      await release.wait()

    manager.stop_container = holds
    manager.request_stop(containers[0].id)
    assert signals == []
    manager.task_queue.dispatch_tick()
    assert signals == pages(containers[0])
    release.set()
    await manager.run()
    assert signals == pages(containers[0]) * 2

  async def test_the_minutely_look_shows_again_only_the_containers_whose_state_changed(self, manager, containers, signals,
                                                                                       clock):
    await deployed(containers[0])
    report(containers[0])
    await manager.observe_containers()
    signals.clear()
    await manager.observe_containers()
    assert signals == []
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    await manager.observe_containers()
    assert signals == pages(containers[0])
    assert view(manager, containers[0]).headline == "checking"

  def test_the_minutely_look_is_quiet_and_holds_no_scope(self, manager):
    manager.request_container_observation()
    [task] = manager.task_queue.snapshot()["queued"]
    assert (task["key"], task["scopes"], task["quiet"]) == (["observe_containers"], [], True)


def queued(manager):
  return [(tuple(task["key"]), task["quiet"], task["scopes"]) for task in manager.task_queue.snapshot()["queued"]]


def on_worker(hostname, path):
  return Path(Tailscale().local(hostname, path))


def place(container, record):
  """A record on the container's worker that Sage does not hold yet."""
  path = on_worker(container.worker_id, record_path(config.WORKER_HOME_DIR, container.id))
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(encode_record(record))


class TestDomainApplies:
  @pytest.fixture
  def domains(self, containers):
    Domain.create(name="other", type="public", port=80, container=containers[1])
    return sorted(Domain.create(name=name, type="public", port=80, container=containers[0]).id for name in ("shop", "api"))

  async def test_the_domains_of_a_container_that_stops_being_routable_or_becomes_it_are_applied(
          self, manager, containers, domains):
    await deployed(containers[0])
    report(containers[0])
    manager.container_state.check()
    assert manager.applied == []
    report(containers[0], state="exited", at=WORKER_CLOCK + timedelta(seconds=20))
    manager.container_state.check()
    assert manager.applied == [domains]
    report(containers[0], at=WORKER_CLOCK + timedelta(seconds=30))
    manager.container_state.check()
    assert manager.applied == [domains, domains]

  async def test_a_container_whose_routability_goes_unknown_and_comes_back_has_nothing_applied(
          self, manager, containers, domains, clock):
    await deployed(containers[0])
    report(containers[0])
    manager.container_state.check()
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    manager.container_state.check()
    assert manager.container_state.last[containers[0].id].routable is None
    report(containers[0], at=WORKER_CLOCK + timedelta(seconds=config.REPORT_STALE_SECONDS + 5))
    manager.container_state.check()
    assert manager.container_state.last[containers[0].id].routable is True
    assert manager.applied == []


class TestReads:
  async def test_the_minutely_look_asks_once_for_the_record_of_a_container_never_read(self, manager, containers):
    await manager.observe_containers()
    assert queued(manager) == [(("read_record", container.id), True, [f"worker:{container.worker_id}:container:{container.id}"])
                               for container in containers]
    await manager.run()
    assert {Containers().target(container.id).kind for container in containers} == {"absent"}
    await manager.observe_containers()
    assert queued(manager) == []

  async def test_a_record_whose_read_failed_is_read_again_only_while_the_worker_reports(self, manager, hosts, containers):
    hosts.unreachable.add("w1")
    await Containers().read(containers[0])
    await manager.observe_containers()
    assert ("read_record", containers[0].id) not in [key for key, _, _ in queued(manager)]
    report(containers[0])
    await manager.observe_containers()
    assert ("read_record", containers[0].id) in [key for key, _, _ in queued(manager)]

  async def test_a_container_with_an_operation_running_is_not_read_beside_it(self, manager, containers):
    release = asyncio.Event()

    async def holds(container_id):
      await release.wait()

    manager.stop_container = holds
    manager.request_stop(containers[0].id)
    manager.task_queue.dispatch_tick()
    await manager.observe_containers()
    assert [key for key, _, _ in queued(manager)] == [("read_record", containers[1].id)]
    release.set()
    await manager.run()

  async def test_a_read_holds_what_the_worker_has(self, manager, containers):
    record = TargetRecord(authority(containers[0]), settled=Settled("s1", "stopped", "succeeded", datetime(2026, 1, 1)))
    place(containers[0], record)
    await manager.read_record(containers[0].id)
    assert Containers().target(containers[0].id) == TargetRead("present", record)

  async def test_a_container_deleted_before_its_read_is_nothing_to_do(self, manager, hosts, containers):
    container_id = containers[0].id
    containers[0].delete_instance()
    await manager.read_record(container_id)
    assert hosts.commands == []


class TestListings:
  async def test_a_listing_asks_for_a_read_only_of_the_containers_whose_record_differs(self, manager, containers, hosts):
    await deployed(containers[0])
    await Containers().read(containers[1])
    place(containers[1], TargetRecord(authority(containers[1])))
    same = Container.create(application=Application.create(project=containers[0].application.project, name="api",
                                                           label="API", image="nginx"), worker=containers[0].worker)
    await deployed(same)
    never_read = Container.create(application=Application.create(project=containers[0].application.project, name="db",
                                                                 label="DB", image="postgres"), worker=containers[0].worker)
    place(containers[0], TargetRecord(authority(containers[0]), settled=Settled("s9", "stopped", "succeeded", datetime(2026, 1, 1))))
    await manager.list_records("w1")
    assert sorted(key for key, _, _ in queued(manager)) == sorted([("read_record", containers[0].id), ("read_record", never_read.id)])
    await manager.list_records("w2")
    assert ("read_record", containers[1].id) in [key for key, _, _ in queued(manager)]

  async def test_a_listing_that_cannot_reach_the_worker_asks_for_nothing(self, manager, containers, hosts):
    hosts.unreachable.add("w1")
    await manager.list_records("w1")
    assert queued(manager) == []

  def test_listings_are_quiet_and_only_for_the_workers_that_report(self, manager, containers):
    report(containers[1])
    manager.request_record_listings()
    assert [(task["key"], task["quiet"], task["scopes"]) for task in manager.task_queue.snapshot()["queued"]] == [
        (["list_records", "w2"], True, [])]


class Monotonic:
  def __init__(self):
    self.now = 1000.0

  def __call__(self):
    return self.now


@pytest.fixture
def monotonic(monkeypatch):
  import services.manager.containers as module
  clock = Monotonic()
  monkeypatch.setattr(module, "monotonic", clock)
  return clock


def requested(manager):
  return [(task["name"], task["params"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]]


class TestCorrections:
  async def test_an_interrupted_operation_is_contained_once_and_settled_after_its_allowance(
          self, manager, containers, monotonic):
    container = containers[0]
    place(container, TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", datetime(2026, 1, 1),
                                                                            905, input_hash="a" * 64)))
    await Containers().read(container)
    report(container)
    await manager.observe_containers()
    assert ("recover", {"container_id": container.id, "incident": "d9", "final": False}, False) in requested(manager)
    manager.task_queue.cancel(scope="worker")
    monotonic.now += 904
    await manager.observe_containers()
    assert [name for name, _, _ in requested(manager) if name == "recover"] == []
    monotonic.now += 1
    await manager.observe_containers()
    assert ("recover", {"container_id": container.id, "incident": "d9", "final": True}, False) in requested(manager)

  async def test_a_restart_waits_the_whole_allowance_again(self, manager, containers, monotonic):
    container = containers[0]
    place(container, TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", datetime(2026, 1, 1),
                                                                            905, input_hash="a" * 64)))
    await Containers().read(container)
    report(container)
    await manager.observe_containers()
    manager.task_queue.cancel(scope="worker")
    monotonic.now += 2000
    manager.recoveries.clear()
    await manager.observe_containers()
    assert [params["final"] for name, params, _ in requested(manager) if name == "recover"] == [False]

  async def test_a_contradicting_runtime_is_reset_visibly(self, manager, containers):
    container = containers[0]
    await Containers().write(container, TargetRecord(authority(container), settled=Settled("s1", "stopped", "succeeded",
                                                                                           datetime(2026, 1, 1))))
    report(container)
    await manager.observe_containers()
    assert ("reset", {"container_id": container.id, "cause": "unexpected_running"}, False) in requested(manager)

  async def test_the_bookkeeping_goes_when_the_record_no_longer_needs_recovery_or_the_container_is_gone(
          self, manager, containers, monotonic):
    for container in containers:
      place(container, TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", datetime(2026, 1, 1),
                                                                              905, input_hash="a" * 64)))
      await Containers().read(container)
      report(container)
    await manager.observe_containers()
    assert set(manager.recoveries) == {container.id for container in containers}
    await deployed(containers[0])
    containers[1].delete_instance()
    await manager.observe_containers()
    assert manager.recoveries == {}


ORPHAN = "0f0f0f0f0f0f0f0f"


def reported_runtime(hostname, container_id, labels):
  reading = ContainerReading(
      name=runtime_name(container_id), status="running", image="nginx", labels=labels, exit_code=0, oom_killed=False,
      started_at=1, finished_at=None, health=None, cpu_percent=None, mem_bytes=None, net_rx_bytes=None, net_tx_bytes=None)
  WorkerReports().accept(hostname, [Snapshot(at=WORKER_CLOCK + timedelta(seconds=10), host=HostReading(*([None] * 13)),
                                             containers={reading.name: reading}, traefik_reachable=True)])


def cleanups(manager):
  return [(task["params"], task["labels"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]
          if task["name"] == "cleanup_orphan"]


class TestOrphans:
  @pytest.mark.parametrize("labelled, found", [("ours", True), ("another Sage's", False), ("unlabelled", False)])
  async def test_a_reported_runtime_no_container_owns_is_an_orphan_when_it_is_labelled_as_this_sages(
          self, manager, containers, labelled, found):
    labels = {"ours": runtime_labels(Authority(Instance.get().id, Instance.get().state_generation, ORPHAN), "d1", "demo-web"),
              "another Sage's": runtime_labels(Authority("ffffffffffffffff", "1111111111111111", ORPHAN), "d1", "demo-web"),
              "unlabelled": {}}[labelled]
    reported_runtime("w1", ORPHAN, labels)
    await manager.observe_containers()
    assert cleanups(manager) == ([({"hostname": "w1", "container_id": ORPHAN},
                                   {"Worker": "w1", "Container": ORPHAN, "Application": "demo-web"}, False)] if found else [])

  async def test_a_reported_runtime_of_a_container_is_not_an_orphan(self, manager, containers):
    report(containers[0])
    await manager.observe_containers()
    assert cleanups(manager) == []

  async def test_a_listing_finds_this_sages_orphan_records_and_bare_directories(self, manager, containers):
    for orphan, authority_ in (("0f0f0f0f0f0f0f0f", Authority(Instance.get().id, "1111111111111111", "0f0f0f0f0f0f0f0f")),
                               ("0e0e0e0e0e0e0e0e", Authority("ffffffffffffffff", "1111111111111111", "0e0e0e0e0e0e0e0e"))):
      place(SimpleNamespace(id=orphan, worker_id="w1"), TargetRecord(authority_))
    on_worker("w1", placement_dir(config.WORKER_HOME_DIR, "0d0d0d0d0d0d0d0d")).mkdir(parents=True)
    on_worker("w1", placement_dir(config.WORKER_HOME_DIR, containers[0].id)).mkdir(parents=True)
    on_worker("w2", placement_dir(config.WORKER_HOME_DIR, "0c0c0c0c0c0c0c0c")).mkdir(parents=True)
    on_worker("w1", placement_dir(config.WORKER_HOME_DIR, containers[1].id)).mkdir(parents=True)
    await manager.list_records("w1")
    assert sorted(params["container_id"] for params, _, _ in cleanups(manager)) == sorted(
        ["0d0d0d0d0d0d0d0d", "0f0f0f0f0f0f0f0f", containers[1].id])

  async def test_an_orphan_waiting_out_old_work_is_not_asked_for_again_until_its_allowance_has_passed(
          self, manager, containers, monotonic):
    reported_runtime("w1", ORPHAN, runtime_labels(Authority(Instance.get().id, Instance.get().state_generation, ORPHAN), "d1",
                                                  "demo-web"))
    manager.orphans[("w1", ORPHAN)] = ("d9", monotonic.now, 905)
    await manager.observe_containers()
    assert cleanups(manager) == []
    monotonic.now += 905
    await manager.observe_containers()
    assert len(cleanups(manager)) == 1
