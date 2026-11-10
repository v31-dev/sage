import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
from services.applications.deploy_input import capture_deploy_config
from services.containers.placement import record_path, runtime_labels, runtime_name
from services.containers.record import Applied, Authority, Operation, Settled, TargetRead, TargetRecord, encode_record
from services.containers.targets import Containers
from services.db.models import Application, Container, Instance, Worker
from services.manager.containers import ContainersMixin
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from services.updates import UpdateSource
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from utils.queue import TaskQueue

WORKER_CLOCK = datetime(2026, 1, 1, 9)


class WorkerHosts:
  """The workers: commands run in a local shell over the test's directory and synced files land there, as SSH and SFTP
  would put them; a worker in `unreachable` cannot be asked."""

  def __init__(self):
    self.unreachable = set()
    self.commands = []

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append((hostname, command))
    if hostname in self.unreachable:
      raise Exception(f"Error connecting to {hostname}: no route")
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True)
    return result.returncode, result.stdout.splitlines()

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    if hostname in self.unreachable:
      raise Exception(f"Error connecting to {hostname}: no route")
    content = Path(src).read_text()
    for key, value in (keys or {}).items():
      content = content.replace("${" + key + "}", str(value))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(content)


class Manager(ContainersMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = self.containers_queue_facts()
    self.container_state = self.containers_state_fact()
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  async def run(self):
    self.task_queue.dispatch_tick()
    while self.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
      self.task_queue.dispatch_tick()


@pytest.fixture
def hosts(register, tmp_path, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", str(tmp_path / "home"))
  return register(Tailscale, WorkerHosts())


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
    state = view(manager, containers[0])
    assert (state.headline, state.config, state.routable) == ("active", "current", True)

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


def place(container, record):
  """A record on the worker that Sage does not hold yet."""
  path = Path(record_path(config.WORKER_HOME_DIR, container.id))
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(encode_record(record))


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
