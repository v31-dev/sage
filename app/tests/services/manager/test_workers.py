import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from services.db import Container, Worker
from services.manager.decisions import WorkerReboot
from services.manager.workers import WorkersMixin
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from utils.executor import active_executor
from utils.logging import TaskFailed

STACK = ("traefik", "vector", "telegraf", "cloudflared")


class FakeTailscale:
  def __init__(self):
    self.listed = []          # (hostname, ip) of the tailnet's machines
    self.answering = set()    # hostnames that answer a ping
    self.asked = None
    self.commands = []
    self.up_output = ["Container traefik  Running"]
    self.up_error = None
    self.synced = []          # (path on the worker, substitutions) of the files written
    self.sync_error = None
    self.forgotten = []       # hostnames whose pooled connection was closed

  def ip(self):
    return "100.64.0.1"

  def peers(self):
    return [SimpleNamespace(hostname=h, ip=ip) for h, ip in self.listed]

  def find_peer(self, query):
    return next((peer for peer in self.peers() if query in (peer.hostname, peer.ip)), None)

  async def ping(self, ip):
    return any(peer.ip == ip and peer.hostname in self.answering for peer in self.peers())

  async def online_states(self, peers):
    self.asked = peers
    return {hostname: hostname in self.answering for hostname in peers}

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append((hostname, command))
    if self.up_error:
      raise self.up_error
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
    self.worker_reboots = {}
    self.queued = set()          # (task name, hostname) of the additions, removals and reboots queued or running
    self.dns_deleted = []
    self.cloudflare = SimpleNamespace(domain="example.test", delete_dns_record=lambda **kw: self.dns_deleted.append(kw))

  def has_task(self, task, params=None):
    return (task.__name__, params["worker_hostname"]) in self.queued

  def notify(self, message, level="info"):
    self.notices.append((level, message))

  def refresh_worker_summary(self):
    pass

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


def add(hostname, online, ip="100.64.0.2"):
  return Worker.create(hostname=hostname, ip=ip, online=online)


class TestSyncWorkers:
  def sync(self, harness, **kwargs):
    asyncio.run(harness.sync_workers(**kwargs))

  def test_the_plan_is_carried_out(self, harness):
    add("w1", True)
    add("w2", False)
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.2")]
    harness.tailscale.answering = {"w2"}
    self.sync(harness)
    assert harness.offlined == ["w1"]
    assert harness.setups == [("w2", False)]

  def test_a_machine_that_is_not_a_known_worker_is_not_pinged_or_set_up(self, harness):
    add("w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w3", "100.64.0.3")]
    harness.tailscale.answering = {"w1", "w3"}
    self.sync(harness)
    assert harness.tailscale.asked == {"w1": ("100.64.0.2", True)}
    assert harness.setups == []

  def test_a_worker_that_does_not_answer_is_taken_out(self, harness):
    add("w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    self.sync(harness)
    assert harness.offlined == ["w1"]

  def test_the_pings_are_given_what_the_database_knows_about_each_worker(self, harness):
    add("w1", True, "100.64.0.2")
    add("w2", False, "100.64.0.3")
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.3")]
    self.sync(harness)
    assert harness.tailscale.asked == {"w1": ("100.64.0.2", True), "w2": ("100.64.0.3", False)}

  def test_a_forced_sync_is_passed_on(self, harness):
    add("w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    harness.tailscale.answering = {"w1"}
    self.sync(harness, force=True)
    assert harness.setups == [("w1", True)]

  def test_one_worker_failing_does_not_stop_the_others(self, harness, caplog):
    add("w1", False)
    add("w2", False, "100.64.0.3")
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
    add("w1", True)
    WorkerReports().accept("w1", snapshot(running=("vector", "telegraf", "cloudflared")))
    harness.tailscale.up_output = ["Container traefik  Starting", "Container traefik  Started"]
    self.repair(harness)
    host, command = harness.tailscale.commands[0]
    assert host == "w1" and "up -d" in command and all(service in command for service in STACK)
    assert harness.notices == [("warning", "Worker w1: traefik not running; started its stack.")]

  def test_a_healthy_stack_is_left_alone(self, harness):
    add("w1", True)
    WorkerReports().accept("w1", snapshot())
    self.repair(harness)
    assert harness.tailscale.commands == []

  def test_a_worker_out_of_the_pool_is_left_alone(self, harness):
    add("w1", False)
    WorkerReports().accept("w1", snapshot(running=()))
    self.repair(harness)
    assert harness.tailscale.commands == []

  def test_a_worker_is_tried_at_most_once_per_cooldown(self, harness, clock):
    add("w1", True)
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
    add("w1", True)
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    harness.queued = {(task, "w1")}
    self.repair(harness)
    assert harness.tailscale.commands == [] and harness.worker_stack_repairs == {}

  def test_a_worker_that_went_silent_in_a_reboot_is_left_alone(self, harness, clock):
    add("w1", True)
    harness.worker_reboots["w1"] = WorkerReboot(clock.current, None)
    clock.advance(timedelta(seconds=config.WORKER_STACK_SILENCE_SECONDS + 1))
    self.repair(harness)
    assert harness.tailscale.commands == [] and harness.worker_stack_repairs == {}

  def test_a_worker_is_forgotten_once_its_cooldown_is_over(self, harness, clock):
    add("w1", True)
    harness.tailscale.up_output = ["Container traefik  Started"]
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    self.repair(harness)
    assert set(harness.worker_stack_repairs) == {"w1"}
    clock.advance(timedelta(seconds=config.WORKER_STACK_REPAIR_COOLDOWN_SECONDS))
    Worker.delete().where(Worker.hostname == "w1").execute()
    self.repair(harness)
    assert harness.worker_stack_repairs == {}

  def test_starting_that_changes_nothing_is_not_announced(self, harness):
    add("w1", True)
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    harness.tailscale.up_output = ["Container traefik  Running"]
    self.repair(harness)
    assert harness.notices == []

  def test_a_start_that_fails_is_reported_and_not_retried_at_once(self, harness):
    add("w1", True)
    WorkerReports().accept("w1", snapshot(traefik_reachable=False))
    harness.tailscale.up_error = Exception("compose failed")
    self.repair(harness)
    self.repair(harness)
    assert harness.notices == [("error", "Worker w1: Traefik is not answering; starting its stack failed: compose failed")]
    assert len(harness.tailscale.commands) == 1

  def test_a_worker_that_never_reports_is_started_after_the_silence(self, harness, clock):
    add("w1", True)
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


class TestWorkerStatus:
  def test_a_worker_that_answers_and_is_not_known_to_be_otherwise_is_online(self, harness):
    assert harness.worker_status("w1", True) == "online"

  def test_a_worker_that_does_not_answer_is_offline(self, harness):
    assert harness.worker_status("w1", False) == "offline"

  def test_a_worker_whose_traefik_does_not_answer_is_not_ready(self, harness):
    WorkerReports().accept("w1", telegraf_started(100, traefik_reachable=False))
    assert harness.worker_status("w1", True) == "not ready"

  def test_a_worker_that_does_not_report_is_not_ready_once_the_manager_has_been_up_a_while(self, harness, clock):
    clock.advance(timedelta(seconds=config.REPORT_STARTUP_GRACE_SECONDS + 1))
    assert harness.worker_status("w1", True) == "not ready"

  def test_a_worker_that_does_not_answer_is_offline_whatever_its_report_says(self, harness):
    WorkerReports().accept("w1", telegraf_started(100, traefik_reachable=False))
    assert harness.worker_status("w1", False) == "offline"

  @pytest.mark.parametrize("online", [True, False])
  def test_a_removal_that_is_queued_or_running_is_removing(self, harness, online):
    harness.queued = {("remove_worker", "w1")}
    assert harness.worker_status("w1", online) == "removing"
    assert harness.worker_status("w2", online) == ("online" if online else "offline")

  def test_removing_wins_over_rebooting(self, harness):
    harness.queued = {("remove_worker", "w1"), ("reboot_worker", "w1")}
    assert harness.worker_status("w1", True) == "removing"

  def test_an_addition_that_is_queued_or_running_is_adding(self, harness):
    harness.queued = {("add_worker", "w1")}
    assert harness.worker_status("w1", False) == "adding"
    assert harness.worker_status("w2", False) == "offline"

  def test_removing_wins_over_adding(self, harness):
    harness.queued = {("remove_worker", "w1"), ("add_worker", "w1")}
    assert harness.worker_status("w1", False) == "removing"

  @pytest.mark.parametrize("online", [True, False])
  def test_a_reboot_that_is_queued_or_running_is_rebooting(self, harness, online):
    harness.queued = {("reboot_worker", "w1")}
    assert harness.worker_status("w1", online) == "rebooting"

  def test_after_the_task_a_reboot_lasts_until_the_worker_reports_with_a_telegraf_that_restarted(self, harness, clock):
    WorkerReports().accept("w1", telegraf_started(100))
    harness.worker_reboots["w1"] = WorkerReboot(clock.current, 100)
    assert harness.worker_status("w1", True) == "rebooting"          # reports go on until the host goes down
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    assert harness.worker_status("w1", False) == "rebooting"         # down: no current report
    clock.advance(timedelta(seconds=60))
    WorkerReports().accept("w1", telegraf_started(100 + 90_000_000_000))
    assert harness.worker_status("w1", True) == "online"

  def test_a_worker_that_is_back_is_not_rebooting_while_its_traefik_does_not_answer(self, harness, clock):
    harness.worker_reboots["w1"] = WorkerReboot(clock.current, 100)
    WorkerReports().accept("w1", telegraf_started(100 + 90_000_000_000, traefik_reachable=False))
    assert harness.worker_status("w1", True) == "not ready"

  def test_a_worker_that_does_not_come_back_is_no_longer_rebooting_after_the_limit(self, harness, clock):
    harness.worker_reboots["w1"] = WorkerReboot(clock.current, None)
    assert harness.worker_status("w1", False) == "rebooting"
    clock.advance(timedelta(seconds=config.WORKER_REBOOT_LIMIT_SECONDS + 1))
    assert harness.worker_status("w1", False) == "offline"


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
    assert harness.notices == [("error", "Failed to add worker w3: it is not in the tailnet or does not answer a ping.")]

  def test_a_machine_that_does_not_answer_a_ping_is_refused_and_said_so(self, harness):
    harness.tailscale.listed = [("w3", "100.64.0.3")]
    with pytest.raises(TaskFailed):
      self.add(harness)
    assert harness.setups == []
    assert harness.notices[0][0] == "error" and "does not answer a ping" in harness.notices[0][1]


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


class TestRemoveWorker:
  def remove(self, harness):
    asyncio.run(harness.remove_worker("w1"))

  def reachable(self, harness, owner=None):
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    harness.tailscale.answering = {"w1"}
    if owner:
      harness.tailscale.up_output = [f"/opt/sage/revisions/owner:{owner} example.test"]

  def test_an_online_worker_of_this_sage_has_its_stack_stopped_and_its_files_removed_before_it_is_forgotten(self, harness):
    add("w1", True)
    self.reachable(harness, owner=harness.instance_id())
    self.remove(harness)
    stop = harness.tailscale.commands[-1][1]
    assert "docker-compose.yml down --remove-orphans" in stop and stop.endswith("&& rm -rf /opt/sage")
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    assert harness.notices == []
    assert harness.dns_deleted == [{"name": "*.int.example.test", "content": "100.64.0.2"}]

  @pytest.mark.parametrize("online", [True, False])
  def test_the_pooled_ssh_connection_of_a_removed_worker_is_closed(self, harness, online):
    add("w1", online)
    if online:
      self.reachable(harness, owner=harness.instance_id())
    self.remove(harness)
    assert harness.tailscale.forgotten == ["w1"]

  def test_a_worker_of_another_sage_is_forgotten_and_left_as_it_is(self, harness):
    add("w1", True)
    self.reachable(harness, owner="someone-else")
    self.remove(harness)
    assert [command for _, command in harness.tailscale.commands if "down" in command] == []
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    assert harness.notices == []

  def test_a_worker_without_an_owner_stamp_is_forgotten_and_left_as_it_is(self, harness):
    add("w1", True)
    self.reachable(harness)
    self.remove(harness)
    assert [command for _, command in harness.tailscale.commands if "down" in command] == []
    assert Worker.get_or_none(Worker.hostname == "w1") is None

  def test_a_worker_that_does_not_answer_is_forgotten_without_cleanup_and_the_user_is_told_to_finish_by_hand(self, harness):
    add("w1", False)
    self.remove(harness)
    assert harness.tailscale.commands == []
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    (level, message), = harness.notices
    assert level == "warning" and "was offline" in message and "by hand" in message

  def test_a_stack_that_cannot_be_stopped_still_lets_the_worker_go_and_says_so(self, harness):
    add("w1", True)
    self.reachable(harness)
    harness.tailscale.up_error = Exception("connection lost")
    self.remove(harness)
    assert Worker.get_or_none(Worker.hostname == "w1") is None
    (level, message), = harness.notices
    assert level == "warning" and "could not be stopped: connection lost" in message

  def test_an_online_worker_with_containers_is_refused_and_nothing_changes(self, harness, application):
    add("w1", True)
    Container.create(application=application, worker="w1")
    with pytest.raises(TaskFailed):
      self.remove(harness)
    assert Worker.get_or_none(Worker.hostname == "w1") is not None
    assert harness.deleted_containers == [] and harness.tailscale.commands == []

  def test_an_offline_worker_has_its_containers_force_deleted_with_it(self, harness, application):
    add("w1", False)
    first = Container.create(application=application, worker="w1")
    self.remove(harness)
    assert harness.deleted_containers == [(first.id, True)]
    assert Worker.get_or_none(Worker.hostname == "w1") is None

  def test_the_files_of_a_worker_that_had_containers_deleted_without_cleanup_are_left_even_if_it_answers(self, harness, application):
    add("w1", False)
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
    add("w1", True)
    WorkerReports().accept("w1", telegraf_started(777))
    asyncio.run(harness.reboot_worker("w1"))
    assert harness.worker_reboots == {"w1": WorkerReboot(clock.current, 777)}
    assert "reboot" in harness.tailscale.commands[0][1]

  def test_the_start_before_the_reboot_is_taken_from_a_report_that_is_a_little_old_too(self, harness, clock):
    add("w1", True)
    WorkerReports().accept("w1", telegraf_started(777))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 5))
    asyncio.run(harness.reboot_worker("w1"))
    assert harness.worker_reboots["w1"].started_at == 777

  def test_a_reboot_that_fails_leaves_no_mark(self, harness):
    add("w1", True)
    harness.tailscale.up_error = Exception("no route")
    with pytest.raises(Exception, match="Failed to reboot worker w1"):
      asyncio.run(harness.reboot_worker("w1"))
    assert harness.worker_reboots == {}

  def test_removing_a_worker_drops_its_mark_and_its_reports(self, harness, clock):
    add("w1", False)
    WorkerReports().accept("w1", telegraf_started(1))
    harness.worker_reboots["w1"] = WorkerReboot(clock.current, 1)
    asyncio.run(harness.remove_worker("w1"))
    assert harness.worker_reboots == {} and WorkerReports().latest("w1") is None
    assert Worker.get_or_none(Worker.hostname == "w1") is None

  def test_the_sync_forgets_the_reboots_that_are_over(self, harness, clock):
    add("w1", True)
    add("w2", True, "100.64.0.3")
    WorkerReports().accept("w1", telegraf_started(100 + 1))
    WorkerReports().accept("w2", telegraf_started(100))
    harness.worker_reboots = {"w1": WorkerReboot(clock.current, 100), "w2": WorkerReboot(clock.current, 100)}
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.3")]
    harness.tailscale.answering = {"w1", "w2"}
    asyncio.run(harness.sync_workers())
    assert set(harness.worker_reboots) == {"w2"}
