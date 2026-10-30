import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from services.db import Container, Worker
from services.logs import Logs
from services.manager._common import routing_input_hash
from services.manager.decisions import WorkerReboot
from services.manager.workers import WorkersMixin
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from utils.executor import active_executor
from utils.logging import TaskFailed

STACK = ("traefik", "vector", "telegraf", "cloudflared")


class FakeTailscale:
  def __init__(self):
    self.listed = []          # (hostname, ip) of the tailnet's machines
    self.answering = set()    # hostnames whose SSH server answers
    self.asked = None         # {hostname: online now} given to online_states
    self.refreshed = None     # the IPs given to refresh_paths
    self.paths = {}           # how the peers were reached at the last refresh_paths
    self.commands = []
    self.up_output = ["Container traefik  Running"]
    self.up_error = None
    self.synced = []          # (path on the worker, substitutions) of the files written
    self.sync_error = None
    self.forgotten = []       # hostnames whose pooled connection was closed
    self.fail_when = None     # a part of a command that makes it fail

  def ip(self):
    return "100.64.0.1"

  def peers(self):
    return [SimpleNamespace(hostname=h, ip=ip) for h, ip in self.listed]

  def find_peer(self, query):
    return next((peer for peer in self.peers() if query in (peer.hostname, peer.ip)), None)

  async def reachable(self, hostname):
    return hostname in self.answering

  async def online_states(self, peers):
    self.asked = peers
    return {hostname: hostname in self.answering for hostname in peers}

  async def refresh_paths(self, ips):
    self.refreshed = ips
    self.paths = {ip: "direct" for ip in ips}

  def path(self, ip):
    return self.paths.get(ip)

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append((hostname, command))
    if self.up_error:
      raise self.up_error
    if self.fail_when and self.fail_when in command:
      raise Exception("compose failed")
    return 0, self.up_output

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=None):
    self.synced.append((dst, keys))
    if self.sync_error:
      raise self.sync_error

  async def forget(self, hostname):
    self.forgotten.append(hostname)


class Harness(WorkersMixin):
  worker_home_dir = "/opt/sage"

  def __init__(self):
    self.tailscale = FakeTailscale()
    self.worker_stack_repairs = {}
    self.notices = []
    self.setups = []
    self.deleted_containers = []
    self.offlined = []
    self.failing = set()
    self._worker_inputs = {}
    self.worker_list = []
    self._worker_list_lock = threading.Lock()
    self.dns_deleted = []
    self.cloudflare = SimpleNamespace(domain="example.test", delete_dns_record=lambda **kw: self.dns_deleted.append(kw))

  def notify(self, message, level="info"):
    self.notices.append((level, message))

  async def setup_worker(self, worker, force=False):
    if worker.hostname in self.failing:
      raise Exception("boom")
    self.setups.append((worker.hostname, force))

  async def delete_container(self, container_id, force=False):
    self.deleted_containers.append((container_id, force))
    Container.delete().where(Container.id == container_id).execute()

  def set_worker_offline(self, worker):
    self.offlined.append(worker.hostname)


@pytest.fixture
def harness(db, clock):
  executor = ThreadPoolExecutor(max_workers=1)
  token = active_executor.set(executor)
  WorkerReports()  # the Manager creates it at startup, which is when its silence starts
  yield Harness()
  active_executor.reset(token)
  executor.shutdown()


def add(harness, hostname, online, ip="100.64.0.2"):
  """A worker with a row, whose online is what the sync observed."""
  harness._save_worker(hostname, ip)
  harness.update_worker(hostname, online=online)
  return Worker.get(Worker.hostname == hostname)


class TestSyncWorkers:
  def sync(self, harness, **kwargs):
    asyncio.run(harness.sync_workers(**kwargs))

  def test_the_plan_is_carried_out(self, harness):
    add(harness, "w1", True)
    add(harness, "w2", False)
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.2")]
    harness.tailscale.answering = {"w2"}
    self.sync(harness)
    assert harness.offlined == ["w1"]
    assert harness.setups == [("w2", False)]

  def test_the_sync_ends_by_working_the_list_out_as_of_now(self, harness, clock):
    add(harness, "w1", True)
    WorkerReports().accept("w1", telegraf_started(100))
    harness.update_worker("w1", report=WorkerReports().latest("w1"))
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    harness.tailscale.answering = {"w1"}
    assert harness.worker_list[0]["container_count"] == 1
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    self.sync(harness)
    assert (harness.worker_list[0]["status"], harness.worker_list[0]["container_count"]) == ("not ready", None)

  def test_a_machine_that_is_not_a_known_worker_is_not_checked_or_set_up(self, harness):
    add(harness, "w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w3", "100.64.0.3")]
    harness.tailscale.answering = {"w1", "w3"}
    self.sync(harness)
    assert harness.tailscale.asked == {"w1": True}
    assert harness.setups == []

  def test_a_worker_that_does_not_answer_is_taken_out(self, harness):
    add(harness, "w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    self.sync(harness)
    assert harness.offlined == ["w1"]

  def test_the_checks_are_given_what_the_database_knows_about_each_worker(self, harness):
    add(harness, "w1", True, "100.64.0.2")
    add(harness, "w2", False, "100.64.0.3")
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.3")]
    self.sync(harness)
    assert harness.tailscale.asked == {"w1": True, "w2": False}

  def test_a_worker_no_sync_has_looked_at_is_checked_as_not_known_and_set_up_once_it_answers(self, harness):
    add(harness, "w1", None)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    harness.tailscale.answering = {"w1"}
    self.sync(harness)
    assert harness.tailscale.asked == {"w1": None}
    assert harness.setups == [("w1", False)] and harness.offlined == []

  def test_a_worker_no_sync_has_looked_at_that_is_found_offline_is_taken_out(self, harness):
    add(harness, "w1", None)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    self.sync(harness)
    assert harness.offlined == ["w1"] and harness.setups == []

  def test_a_forced_sync_is_passed_on(self, harness):
    add(harness, "w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    harness.tailscale.answering = {"w1"}
    self.sync(harness, force=True)
    assert harness.setups == [("w1", True)]

  def test_one_worker_failing_does_not_stop_the_others(self, harness, caplog):
    add(harness, "w1", False)
    add(harness, "w2", False, "100.64.0.3")
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.3")]
    harness.tailscale.answering = {"w1", "w2"}
    harness.failing = {"w1"}
    self.sync(harness)
    assert harness.setups == [("w2", False)]
    assert "sync_workers failed for w1: boom" in caplog.text


def snapshot(running=STACK, traefik_reachable=True):
  def reading(name):
    return ContainerReading(name=name, status="running", image="", deployed_at=None, exit_code=0, oom_killed=False,
                            started_at=1, finished_at=None, health=None, cpu_percent=None, mem_bytes=None,
                            net_rx_bytes=None, net_tx_bytes=None)
  return [Snapshot(at=datetime(2026, 1, 1, 12), host=HostReading(*([None] * 13)),
                   containers={n: reading(n) for n in running}, traefik_reachable=traefik_reachable)]


class TestRepairWorkerStacks:
  def repair(self, harness):
    asyncio.run(harness.repair_worker_stacks())

  def test_a_stack_the_report_shows_down_is_started_and_said_so(self, harness):
    add(harness, "w1", True)
    WorkerReports().accept("w1", snapshot(running=("vector", "telegraf", "cloudflared")))
    harness.tailscale.up_output = ["Container traefik  Starting", "Container traefik  Started"]
    self.repair(harness)
    host, command = harness.tailscale.commands[0]
    assert host == "w1" and "up -d" in command and all(service in command for service in STACK)
    assert harness.notices == [("warning", "Worker w1: traefik not running; started its stack.")]

  def test_a_healthy_stack_is_left_alone(self, harness):
    add(harness, "w1", True)
    WorkerReports().accept("w1", snapshot())
    self.repair(harness)
    assert harness.tailscale.commands == []

  def test_a_worker_out_of_the_pool_is_left_alone(self, harness):
    add(harness, "w1", False)
    WorkerReports().accept("w1", snapshot(running=()))
    self.repair(harness)
    assert harness.tailscale.commands == []

  def test_a_worker_is_tried_at_most_once_per_cooldown(self, harness, clock):
    add(harness, "w1", True)
    harness.tailscale.up_output = ["Container traefik  Started"]
    down = snapshot(traefik_reachable=False)
    WorkerReports().accept("w1", down)
    self.repair(harness)
    clock.advance(timedelta(seconds=config.WORKER_STACK_REPAIR_COOLDOWN_SECONDS - 1))
    WorkerReports().accept("w1", down)
    self.repair(harness)
    assert len(harness.tailscale.commands) == 1
    clock.advance(timedelta(seconds=1))
    WorkerReports().accept("w1", down)
    self.repair(harness)
    assert len(harness.tailscale.commands) == 2

  @pytest.mark.parametrize("task", ["reboot_worker", "remove_worker"])
  def test_a_worker_that_is_being_rebooted_or_removed_is_left_alone(self, harness, task):
    add(harness, "w1", True)
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    harness._set_worker_tasks(((task, "w1"),))
    self.repair(harness)
    assert harness.tailscale.commands == [] and harness.worker_stack_repairs == {}

  def test_a_worker_that_went_silent_in_a_reboot_is_left_alone(self, harness, clock):
    add(harness, "w1", True)
    harness.update_worker("w1", reboot=WorkerReboot(clock.current, None))
    clock.advance(timedelta(seconds=config.WORKER_STACK_SILENCE_SECONDS + 1))
    self.repair(harness)
    assert harness.tailscale.commands == [] and harness.worker_stack_repairs == {}

  def test_a_worker_is_forgotten_once_its_cooldown_is_over(self, harness, clock):
    add(harness, "w1", True)
    harness.tailscale.up_output = ["Container traefik  Started"]
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    self.repair(harness)
    assert set(harness.worker_stack_repairs) == {"w1"}
    clock.advance(timedelta(seconds=config.WORKER_STACK_REPAIR_COOLDOWN_SECONDS))
    Worker.delete().where(Worker.hostname == "w1").execute()
    self.repair(harness)
    assert harness.worker_stack_repairs == {}

  def test_starting_that_changes_nothing_is_not_announced(self, harness):
    add(harness, "w1", True)
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    harness.tailscale.up_output = ["Container traefik  Running"]
    self.repair(harness)
    assert harness.notices == []

  def test_a_start_that_fails_is_reported_and_not_retried_at_once(self, harness):
    add(harness, "w1", True)
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    harness.tailscale.up_error = Exception("compose failed")
    self.repair(harness)
    self.repair(harness)
    assert harness.notices == [("error", "Worker w1: Traefik is not answering; starting its stack failed: compose failed")]
    assert len(harness.tailscale.commands) == 1

  def test_a_worker_that_never_reports_is_started_after_the_silence(self, harness, clock):
    add(harness, "w1", True)
    clock.advance(timedelta(seconds=config.WORKER_STACK_SILENCE_SECONDS))
    self.repair(harness)
    assert harness.tailscale.commands == []
    clock.advance(timedelta(seconds=1))
    self.repair(harness)
    assert len(harness.tailscale.commands) == 1


class TestStartWorkerStack:
  @pytest.mark.parametrize("output, started", [
      (["Container traefik  Running", "Container vector  Running"], False),
      (["Container traefik  Started"], True),
      (["Container traefik  Starting"], True),
      (["Container telegraf  Created", "Container telegraf  Started"], True),
      ([], False),
  ])
  def test_it_says_whether_something_was_started(self, harness, output, started):
    harness.tailscale.up_output = output
    worker = SimpleNamespace(hostname="w1")
    assert asyncio.run(harness.start_worker_stack(worker)) is started


def telegraf_started(started_at, traefik_reachable=True):
  from dataclasses import replace
  reading = ContainerReading(name="telegraf", status="running", image="", deployed_at=None, exit_code=0, oom_killed=False,
                             started_at=started_at, finished_at=None, health=None, cpu_percent=None, mem_bytes=None,
                             net_rx_bytes=None, net_tx_bytes=None)
  base = snapshot(running=())[0]
  return [replace(base, containers={"telegraf": reading}, traefik_reachable=traefik_reachable)]


class TestAddWorker:
  def add(self, harness):
    asyncio.run(harness.add_worker("w3"))

  def test_a_machine_that_answers_is_set_up(self, harness):
    harness.tailscale.listed = [("w3", "100.64.0.3")]
    harness.tailscale.answering = {"w3"}
    self.add(harness)
    assert harness.setups == [("w3", False)]

  def test_a_machine_that_is_not_in_the_tailnet_is_refused_and_said_so(self, harness):
    with pytest.raises(TaskFailed):
      self.add(harness)
    assert harness.setups == []
    assert harness.notices == [("error", "Failed to add worker w3: it is not in the tailnet or its SSH server does not answer.")]

  def test_a_machine_whose_ssh_server_does_not_answer_is_refused_and_said_so(self, harness):
    harness.tailscale.listed = [("w3", "100.64.0.3")]
    with pytest.raises(TaskFailed):
      self.add(harness)
    assert harness.setups == []
    assert harness.notices[0][0] == "error" and "SSH server does not answer" in harness.notices[0][1]


class TestSetWorkerOffline:
  class Offlining(Harness):
    set_worker_offline = WorkersMixin.set_worker_offline

  def test_the_worker_is_no_longer_online_and_its_dns_record_and_the_list_follow(self, db, clock):
    WorkerReports()
    harness = self.Offlining()
    worker = add(harness, "w1", True)

    harness.set_worker_offline(worker)

    assert harness.is_worker_online("w1") is False
    assert harness.worker_list[0]["status"] == "offline"
    assert harness.dns_deleted == [{"name": "*.int.example.test", "content": "100.64.0.2"}]
    assert harness.notices == [("error", "Worker w1 is offline.")]


class TestRequestStaleApplicationSyncs:
  class Requesting(Harness):
    request_stale_application_syncs = WorkersMixin.request_stale_application_syncs

    def __init__(self):
      super().__init__()
      self.requested = []

    def request_application_traefik_sync(self, application):
      self.requested.append(application.qualified_name)

  @pytest.fixture
  def harness(self, db, clock):
    WorkerReports()
    return self.Requesting()

  def stamp(self, harness, application):
    return routing_input_hash("example.test", list(application.domains), list(application.containers), harness.is_worker_online)

  def test_an_application_whose_stamp_is_what_its_files_would_be_is_left_alone(self, harness, application, worker):
    Container.create(application=application, worker=worker, status="active")
    harness.load_workers()
    harness.update_worker("w1", online=True)
    stamps = {application.qualified_name: self.stamp(harness, application)}

    assert harness.request_stale_application_syncs("example.test", stamps) == []
    assert harness.requested == []

  def test_an_application_with_another_stamp_or_none_is_requested_and_named(self, harness, application, worker):
    assert harness.request_stale_application_syncs("example.test", {application.qualified_name: "old"}) == [
        application.qualified_name]
    assert harness.request_stale_application_syncs("example.test", {}) == [application.qualified_name]
    assert harness.requested == [application.qualified_name] * 2

  def test_the_stamp_follows_whether_the_worker_the_container_is_on_is_online(self, harness, application, worker):
    Container.create(application=application, worker=worker, status="active")
    harness.load_workers()
    harness.update_worker("w1", online=True)
    stamps = {application.qualified_name: self.stamp(harness, application)}

    harness.update_worker("w1", online=False)

    assert harness.request_stale_application_syncs("example.test", stamps) == [application.qualified_name]


class TestRefreshWorkerPaths:
  def test_only_the_online_workers_are_asked(self, harness):
    add(harness, "w1", True, "100.64.0.2")
    add(harness, "w2", False, "100.64.0.3")
    asyncio.run(harness.refresh_worker_paths())
    assert harness.tailscale.refreshed == ["100.64.0.2"]

  def test_the_list_shows_how_each_worker_was_reached(self, harness):
    add(harness, "w1", True, "100.64.0.2")
    add(harness, "w2", False, "100.64.0.3")
    asyncio.run(harness.refresh_worker_paths())
    assert [(worker["hostname"], worker["path"]) for worker in harness.worker_list] == [("w1", "direct"), ("w2", None)]


class SetupHarness(Harness):
  """The real `setup_worker`, up to the first file it writes (the fake tailnet then stops it)."""
  setup_worker = WorkersMixin.setup_worker

  version = "test"

  def __init__(self):
    super().__init__()
    self.cloudflare.tunnel_id = "tunnel"
    self.cloudflare.connect = lambda: None
    self.cloudflare.create_dns_record = lambda **kwargs: None
    self.cloudflare.get_tunnel_token = lambda: SimpleNamespace(token="token")
    self.tailscale.sync_error = Exception("stop here")


class TestSetupWorkerClaim:
  @pytest.fixture
  def setup(self, db, clock):
    executor = ThreadPoolExecutor(max_workers=1)
    token = active_executor.set(executor)
    WorkerReports()
    yield SetupHarness()
    active_executor.reset(token)
    executor.shutdown()

  def run(self, setup, **kwargs):
    asyncio.run(setup.setup_worker(SimpleNamespace(hostname="w3", ip="100.64.0.3"), **kwargs))

  def stamp(self, setup, owner):
    setup.tailscale.up_output = [f"/opt/sage/revisions/owner:{owner} other.test"]

  def test_a_worker_of_another_sage_is_refused_before_anything_is_changed(self, setup):
    self.stamp(setup, "someone-else")
    with pytest.raises(Exception, match=r"managed by another Sage \(other.test\); remove it from there first"):
      self.run(setup)
    assert len(setup.tailscale.commands) == 1 and "grep" in setup.tailscale.commands[0][1]
    assert setup.tailscale.synced == []
    assert Worker.get_or_none(Worker.hostname == "w3") is None
    assert setup.notices[0][0] == "error" and "Failed to setup worker w3" in setup.notices[0][1]

  def test_a_forced_setup_does_not_wipe_the_stamps_of_a_worker_of_another_sage(self, setup):
    self.stamp(setup, "someone-else")
    with pytest.raises(Exception, match="managed by another Sage"):
      self.run(setup, force=True)
    assert [command for _, command in setup.tailscale.commands if "rm -rf" in command] == []

  def test_a_worker_nobody_owns_is_claimed_first(self, setup):
    with pytest.raises(Exception, match="stop here"):
      self.run(setup)
    assert setup.tailscale.synced[0] == ("/opt/sage/revisions/owner", {"CONTENT": f"{setup.instance_id()} example.test"})

  def test_a_worker_already_claimed_by_this_sage_is_not_claimed_again(self, setup):
    setup.tailscale.up_output = [f"/opt/sage/revisions/owner:{setup.instance_id()} example.test"]
    with pytest.raises(Exception, match="stop here"):
      self.run(setup)
    assert all(not path.endswith("/owner") for path, _ in setup.tailscale.synced)

  def test_a_claim_with_an_old_domain_is_rewritten_with_the_current_one(self, setup):
    setup.tailscale.up_output = [f"/opt/sage/revisions/owner:{setup.instance_id()} old.test"]
    with pytest.raises(Exception, match="stop here"):
      self.run(setup)
    assert setup.tailscale.synced[0][1] == {"CONTENT": f"{setup.instance_id()} example.test"}

  def test_a_forced_setup_of_its_own_worker_wipes_the_stamps_and_claims_it_again(self, setup):
    setup.tailscale.up_output = [f"/opt/sage/revisions/owner:{setup.instance_id()} example.test"]
    with pytest.raises(Exception, match="stop here"):
      self.run(setup, force=True)
    assert any(command.endswith("rm -rf /opt/sage/revisions") for _, command in setup.tailscale.commands)
    assert setup.tailscale.synced[0][0] == "/opt/sage/revisions/owner"


class CompleteSetupHarness(SetupHarness):
  """The real `setup_worker` from the claim to the infra stamp: the fake tailnet answers every command."""

  def __init__(self):
    super().__init__()
    self.tailscale.sync_error = None
    self.certs = SimpleNamespace(workers=SimpleNamespace(is_valid=lambda: False))
    self.dns_created = []
    self.cloudflare.create_dns_record = lambda **kwargs: self.dns_created.append(kwargs)
    self.traffic_syncs = []

  def request_application_traefik_sync(self, application):
    self.traffic_syncs.append(application.qualified_name)


class TestSetupWorkerToTheEnd:
  @pytest.fixture
  def setup(self, db, clock):
    executor = ThreadPoolExecutor(max_workers=1)
    token = active_executor.set(executor)
    WorkerReports()
    yield CompleteSetupHarness()
    active_executor.reset(token)
    executor.shutdown()

  def run(self, setup, hostname="w3", ip="100.64.0.3", **kwargs):
    asyncio.run(setup.setup_worker(SimpleNamespace(hostname=hostname, ip=ip), **kwargs))

  def test_a_new_worker_is_in_the_table_and_the_list_and_online_and_said_so(self, setup):
    self.run(setup)
    assert Worker.get(Worker.hostname == "w3").ip == "100.64.0.3"
    assert [(worker["hostname"], worker["ip"], worker["online"], worker["status"]) for worker in setup.worker_list] == [
        ("w3", "100.64.0.3", True, "online")]
    assert setup.notices == [("info", "Worker w3 added.")]

  def test_the_infra_stamp_is_written_last_and_the_dns_record_is_made(self, setup):
    self.run(setup)
    assert setup.tailscale.synced[-1][0] == "/opt/sage/revisions/infra"
    assert setup.dns_created == [{"name": "*.int.example.test", "content": "100.64.0.3", "comment": "sage-worker-w3", "type": "A"}]

  def test_a_worker_that_was_offline_is_online_again_with_its_new_ip(self, setup):
    add(setup, "w3", False, "100.64.0.9")
    self.run(setup)
    assert Worker.get(Worker.hostname == "w3").ip == "100.64.0.3"
    assert [(worker["ip"], worker["online"]) for worker in setup.worker_list] == [("100.64.0.3", True)]
    assert setup.notices == [("info", "Worker w3 synced.")]

  def test_the_applications_whose_stamp_is_stale_are_asked_to_sync_once_the_worker_is_online(self, setup, application, worker):
    setup.load_workers()
    self.run(setup, hostname="w1", ip="100.64.0.2")
    assert setup.traffic_syncs == [application.qualified_name]

  def test_a_new_worker_whose_setup_fails_is_forgotten_again_and_never_online(self, setup):
    setup.tailscale.fail_when = "up -d"
    with pytest.raises(Exception, match="Failed to setup worker w3"):
      self.run(setup)
    assert Worker.get_or_none(Worker.hostname == "w3") is None
    assert setup.worker_list == [] and setup.is_worker_online("w3") is False
    assert setup.notices[0][0] == "error"

  def test_a_worker_that_was_offline_and_whose_setup_fails_stays_offline_and_in_the_table(self, setup):
    add(setup, "w3", False, "100.64.0.3")
    setup.tailscale.fail_when = "up -d"
    with pytest.raises(Exception, match="Failed to sync worker w3"):
      self.run(setup)
    assert Worker.get_or_none(Worker.hostname == "w3") is not None
    assert setup.is_worker_online("w3") is False


class TestRemoveWorker:
  def remove(self, harness):
    asyncio.run(harness.remove_worker("w1"))

  def reachable(self, harness, owner=None):
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    harness.tailscale.answering = {"w1"}
    if owner:
      harness.tailscale.up_output = [f"/opt/sage/revisions/owner:{owner} example.test"]

  def test_the_worker_is_gone_from_the_list_once_it_is_forgotten(self, harness):
    add(harness, "w1", False)
    self.remove(harness)
    assert harness.worker_list == []

  def test_an_online_worker_of_this_sage_has_its_stack_stopped_and_its_files_removed_before_it_is_forgotten(self, harness):
    add(harness, "w1", True)
    self.reachable(harness, owner=harness.instance_id())
    self.remove(harness)
    stop = harness.tailscale.commands[-1][1]
    assert "docker-compose.yml down --remove-orphans" in stop and stop.endswith("&& rm -rf /opt/sage")
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    assert harness.notices == []
    assert harness.dns_deleted == [{"name": "*.int.example.test", "content": "100.64.0.2"}]

  @pytest.mark.parametrize("online", [True, False])
  def test_the_pooled_ssh_connection_of_a_removed_worker_is_closed(self, harness, online):
    add(harness, "w1", online)
    if online:
      self.reachable(harness, owner=harness.instance_id())
    self.remove(harness)
    assert harness.tailscale.forgotten == ["w1"]

  def test_a_worker_of_another_sage_is_forgotten_and_left_as_it_is(self, harness):
    add(harness, "w1", True)
    self.reachable(harness, owner="someone-else")
    self.remove(harness)
    assert [command for _, command in harness.tailscale.commands if "down" in command] == []
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    assert harness.notices == []

  def test_a_worker_without_an_owner_stamp_is_forgotten_and_left_as_it_is(self, harness):
    add(harness, "w1", True)
    self.reachable(harness)
    self.remove(harness)
    assert [command for _, command in harness.tailscale.commands if "down" in command] == []
    assert Worker.get_or_none(Worker.hostname == "w1") is None

  def test_a_worker_that_does_not_answer_is_forgotten_without_cleanup_and_the_user_is_told_to_finish_by_hand(self, harness):
    add(harness, "w1", False)
    self.remove(harness)
    assert harness.tailscale.commands == []
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    (level, message), = harness.notices
    assert level == "warning" and "was offline" in message and "by hand" in message

  def test_a_stack_that_cannot_be_stopped_still_lets_the_worker_go_and_says_so(self, harness):
    add(harness, "w1", True)
    self.reachable(harness)
    harness.tailscale.up_error = Exception("connection lost")
    self.remove(harness)
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    (level, message), = harness.notices
    assert level == "warning" and "could not be stopped: connection lost" in message

  def test_an_online_worker_with_containers_is_refused_and_nothing_changes(self, harness, application):
    add(harness, "w1", True)
    Container.create(application=application, worker="w1")
    with pytest.raises(TaskFailed):
      self.remove(harness)
    assert Worker.get_or_none(Worker.hostname == "w1") is not None
    assert harness.deleted_containers == [] and harness.tailscale.commands == []

  def test_a_worker_no_sync_has_looked_at_with_containers_is_refused_like_an_online_one(self, harness, application):
    add(harness, "w1", None)
    Container.create(application=application, worker="w1")
    with pytest.raises(TaskFailed):
      self.remove(harness)
    assert harness.deleted_containers == [] and harness.tailscale.commands == []

  def test_an_offline_worker_has_its_containers_force_deleted_with_it(self, harness, application):
    add(harness, "w1", False)
    first = Container.create(application=application, worker="w1")
    self.remove(harness)
    assert harness.deleted_containers == [(first.id, True)]
    assert Worker.get_or_none(Worker.hostname == "w1") is None

  def test_the_files_of_a_worker_that_had_containers_deleted_without_cleanup_are_left_even_if_it_answers(self, harness, application):
    add(harness, "w1", False)
    Container.create(application=application, worker="w1")
    self.reachable(harness, owner=harness.instance_id())
    self.remove(harness)
    assert harness.tailscale.commands == []
    assert harness.notices[0][0] == "warning" and "was offline" in harness.notices[0][1]


class TestRebootAndRemove:
  @pytest.fixture(autouse=True)
  def no_waiting(self, monkeypatch):
    async def instant(seconds):
      pass
    monkeypatch.setattr(asyncio, "sleep", instant)

  def test_a_reboot_leaves_a_mark_with_the_start_of_telegraf_before_it(self, harness, clock):
    add(harness, "w1", True)
    WorkerReports().accept("w1", telegraf_started(777))
    asyncio.run(harness.reboot_worker("w1"))
    assert harness._worker_inputs["w1"].reboot == WorkerReboot(clock.current, 777)
    assert "reboot" in harness.tailscale.commands[0][1]

  def test_a_reboot_is_marked_in_the_logs_of_each_container_of_the_worker(self, harness, application, thread_connections):
    worker = add(harness, "w1", True)
    Container.create(application=application, worker=worker)

    asyncio.run(harness.reboot_worker("w1"))

    assert [(line["hostname"], line["stream"], line["message"])
            for line in Logs().query_application_lines(application.qualified_name)["lines"]] == [
        ("w1", "sage", "Worker rebooted by Sage")]

  def test_the_start_before_the_reboot_is_taken_from_a_report_that_is_a_little_old_too(self, harness, clock):
    add(harness, "w1", True)
    WorkerReports().accept("w1", telegraf_started(777))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 5))
    asyncio.run(harness.reboot_worker("w1"))
    assert harness._worker_inputs["w1"].reboot.started_at == 777

  def test_a_reboot_that_fails_leaves_no_mark(self, harness):
    add(harness, "w1", True)
    harness.tailscale.up_error = Exception("no route")
    with pytest.raises(Exception, match="Failed to reboot worker w1"):
      asyncio.run(harness.reboot_worker("w1"))
    assert harness._worker_inputs["w1"].reboot is None

  def test_removing_a_worker_drops_its_mark_and_its_reports(self, harness, clock):
    add(harness, "w1", False)
    WorkerReports().accept("w1", telegraf_started(1))
    harness.update_worker("w1", reboot=WorkerReboot(clock.current, 1))
    asyncio.run(harness.remove_worker("w1"))
    assert harness._worker_inputs == {} and WorkerReports().latest("w1") is None
    assert Worker.get_or_none(Worker.hostname == "w1") is None
