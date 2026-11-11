import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from services.certs import Certs
from services.db.models import Container, Domain, Worker, WorkerEvent
from services.manager.queue import SCOPES, QueueMixin
from services.manager.workers import WorkersMixin
from services.tailscale import Tailscale
from services.updates import UpdateSource
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from services.workers.state import Workers
from utils.queue import TaskQueue


class Tailnet:
  """The workers' observation: `answers` is what each worker's SSH server will be found to be at the next observation,
  `addresses` each worker's address; the paths asked for are kept."""

  def __init__(self):
    self.answers, self.addresses, self.found, self.pinged = {}, {}, {}, []

  async def observe_workers(self, hostnames):
    self.found = {hostname: self.answers.get(hostname, "unobserved") for hostname in hostnames}

  def ssh(self, hostname):
    return self.found.get(hostname, "unobserved")

  def address(self, hostname):
    return self.addresses.get(hostname)

  def path(self, address):
    return None

  async def refresh_paths(self, addresses):
    self.pinged.append(sorted(addresses))


class Manager(WorkersMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = self.workers_queue_facts()
    self.worker_state = self.workers_state_fact()
    self.setup_failures, self.stack_repairs = {}, {}
    self.discovered = 0
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  def request_discovery(self):
    self.discovered += 1


@pytest.fixture
def certificates(register):
  """What the certificates give each worker's state, which a test can change."""
  shown = {}
  register(Certs, SimpleNamespace(worker_state=lambda hostname, subdomain, applied, template: dict(shown.get(hostname, {}))))
  return shown


@pytest.fixture
def tailnet(register, clock, certificates):
  return register(Tailscale, Tailnet())


@pytest.fixture
def manager(tailnet):
  manager = Manager()
  yield manager
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def workers(db):
  return [Worker.create(hostname=hostname, subdomain=subdomain) for hostname, subdomain in (("w1", "red"), ("w2", "blue"))]


def ready(hostname):
  """A worker set up and reporting a working stack."""
  Workers().record_setup(hostname, "succeeded")
  readings = {name: ContainerReading(name=name, status="running", image="", labels={}, exit_code=0, oom_killed=False, started_at=1,
                                     finished_at=None, health=None, cpu_percent=None, mem_bytes=None, net_rx_bytes=None,
                                     net_tx_bytes=None) for name in config.WORKER_STACK_SERVICES}
  WorkerReports().accept(hostname, [Snapshot(at=datetime(2026, 1, 1, 9), host=HostReading(*([None] * 13)), containers=readings,
                                             traefik_reachable=True)])


def pages(*hostnames):
  return [(UpdateSource.WORKER_LIST, None), *((UpdateSource.WORKER_ENTRY, hostname) for hostname in hostnames)]


def events():
  return [(event.subject_id, event.action, event.transition, event.reason) for event in WorkerEvent.select().order_by(WorkerEvent.id)]


class TestObservation:
  async def test_the_workers_found_are_shown_with_what_was_found(self, manager, tailnet, workers):
    tailnet.answers = {"w1": "reachable", "w2": "unreachable"}
    ready("w1")
    await manager.observe_workers()
    states = manager.worker_states()
    assert (states["w1"].view.status, states["w2"].view.status) == ("online", "offline")

  async def test_a_worker_whose_ssh_server_stopped_answering_is_set_up_again_when_it_returns(self, manager, tailnet, workers):
    ready("w1")
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    assert Workers().setup("w1") is None
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    assert (manager.worker_states()["w1"].view.status, manager.worker_states()["w1"].view.reason) == ("checking", "unobserved")

  async def test_paths_are_asked_only_of_reachable_workers_with_an_address(self, manager, tailnet, workers):
    tailnet.answers = {"w1": "reachable", "w2": "unreachable"}
    tailnet.addresses = {"w1": "100.64.0.2", "w2": "100.64.0.3"}
    await manager.observe_workers()
    await manager.refresh_worker_paths()
    assert tailnet.pinged == [["100.64.0.2"]]

  @pytest.mark.parametrize("request_name, key", [("request_worker_observation", ["observe_workers"]),
                                                 ("request_worker_paths", ["refresh_worker_paths"])])
  def test_observation_tasks_are_quiet_and_hold_no_scope(self, manager, request_name, key):
    getattr(manager, request_name)()
    [task] = manager.task_queue.snapshot()["queued"]
    assert (task["key"], task["scopes"], task["quiet"]) == (key, [], True)


class TestPublication:
  async def test_only_the_workers_whose_state_changed_are_shown_again(self, manager, tailnet, workers, signals):
    tailnet.answers = {"w1": "reachable", "w2": "reachable"}
    ready("w1")
    ready("w2")
    await manager.observe_workers()
    signals.clear()
    await manager.observe_workers()
    assert signals == []
    tailnet.answers["w2"] = "unreachable"
    await manager.observe_workers()
    assert signals == pages("w2")

  async def test_a_worker_whose_readiness_changed_shows_the_domains_routed_to_it_again(
          self, manager, tailnet, workers, application, signals):
    Domain.create(name="shop", type="public", port=80, container=Container.create(application=application, worker="w2"))
    Domain.create(name="api", type="internal", port=80, namespace_worker="w2")
    Domain.create(name="other", type="internal", port=80, container=Container.create(application=application, worker="w1"),
                  namespace_worker="w1")
    tailnet.answers = {"w1": "reachable", "w2": "reachable"}
    ready("w1")
    ready("w2")
    await manager.observe_workers()
    signals.clear()
    tailnet.answers["w2"] = "unreachable"
    await manager.observe_workers()
    assert signals[:2] == pages("w2")
    assert sorted(signals[2:]) == sorted([(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                                          (UpdateSource.WORKER_CONTAINERS, "w2"), (UpdateSource.DOMAINS, None)])

  async def test_a_change_of_its_certificates_shows_its_worker_again(self, manager, tailnet, workers, signals, certificates):
    await manager.observe_workers()
    signals.clear()
    certificates["w1"] = {"worker": {"covered": True, "synced": True}}
    manager.worker_state.check()
    assert signals == pages("w1")

  async def test_a_report_shows_its_worker_again_when_its_state_changed(self, manager, tailnet, workers, signals):
    tailnet.answers = {"w1": "reachable"}
    Workers().record_setup("w1", "succeeded")
    await manager.observe_workers()
    signals.clear()
    ready("w1")
    manager.worker_reported("w1")
    assert signals == pages("w1")

  async def test_a_worker_task_queued_and_ended_shows_its_worker(self, manager, tailnet, workers, signals):
    release = asyncio.Event()

    async def reboot_worker(hostname):
      await release.wait()

    manager.add_task(task=reboot_worker, key=("reboot_worker", "w1"), scopes={"worker:w1"}, executor="app", params={"hostname": "w1"})
    assert signals == pages("w1")
    assert manager.worker_states()["w1"].view.status == "rebooting"
    manager.task_queue.dispatch_tick()
    release.set()
    while manager.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
    assert signals == pages("w1") * 2


class TestDiscoveryRequests:
  async def test_a_worker_becoming_reachable_and_set_up_ready_or_unreachable_republishes_discovery(self, manager, tailnet, workers):
    tailnet.answers, tailnet.addresses = {"w1": "reachable"}, {"w1": "100.64.0.2"}
    await manager.observe_workers()
    assert manager.discovered == 0
    Workers().record_setup("w1", "succeeded")
    manager.worker_reported("w1")
    ready("w1")
    manager.worker_reported("w1")
    tailnet.answers["w1"] = "unreachable"
    await manager.observe_workers()
    assert manager.discovered == 3

  async def test_a_new_address_republishes_it_and_a_check_that_changes_neither_set_does_not(self, manager, tailnet, workers):
    tailnet.answers, tailnet.addresses = {"w1": "reachable"}, {"w1": "100.64.0.2"}
    ready("w1")
    await manager.observe_workers()
    before = manager.discovered
    await manager.observe_workers()
    assert manager.discovered == before
    tailnet.addresses["w1"] = "100.64.0.9"
    await manager.observe_workers()
    assert manager.discovered == before + 1


class TestEvents:
  async def test_observed_changes_between_offline_not_ready_and_online_are_recorded_once_each(
          self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    await manager.observe_workers()
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "reachable"}
    Workers().record_setup("w1", "failed")
    await manager.observe_workers()
    assert [(subject, action, reason) for subject, action, _, reason in events()] == [
        ("w1", "connectivity", {"status": "offline", "reason": "ssh_unreachable"}),
        ("w1", "connectivity", {"status": "not ready", "reason": "setup_failed"})]
    assert {transition for _, _, transition, _ in events()} == {"observed"}
    assert notices() == [("error", "Worker w1 is offline.", "https://manager.core.example.test/workers/w1"),
                         ("success", "Worker w1 is reachable again.", "https://manager.core.example.test/workers/w1")]

  async def test_a_worker_that_answers_again_says_so_once_while_it_is_set_up_again(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    ready("w1")
    manager.worker_reported("w1")
    assert manager.worker_state.last["w1"].view.status == "online"
    assert [content for _, content, _ in notices()] == ["Worker w1 is offline.", "Worker w1 is reachable again."]

  async def test_a_worker_offline_when_first_observed_or_unknown_to_the_manager_sends_nothing(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "unknown"}
    await manager.observe_workers()
    assert notices() == []

  async def test_a_worker_first_seen_or_only_checked_records_nothing(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    ready("w1")
    await manager.observe_workers()
    assert events() == [] and notices() == []

  async def test_a_change_without_going_offline_is_recorded_as_readiness(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    Workers().record_setup("w1", "refused")
    manager.worker_reported("w1")
    assert events() == [("w1", "readiness", "observed", {"status": "not ready", "reason": "owned_elsewhere"})]
    assert notices() == []
