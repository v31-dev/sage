import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

import config
from services.applications.deploy_input import capture_deploy_config
from services.containers.placement import runtime_labels, runtime_name
from services.containers.record import Applied, Authority, Operation, Settled, TargetRecord
from services.containers.targets import Containers
from services.db.models import Application, Container, Instance, Worker
from services.manager.containers import ContainersMixin
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from services.updates import UpdateSource
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from utils.queue import TaskQueue

WORKER_CLOCK = datetime(2026, 1, 1, 9)


class Worker1:
  """A worker that takes every record written to it."""

  async def sync_file(self, *args, **kwargs):
    pass


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
def manager(register, clock):
  register(Tailscale, Worker1())
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
