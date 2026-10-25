import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from services.db import Worker
from services.manager.workers import WorkersMixin
from services.reports import ContainerReading, HostReading, Reports, Snapshot
from utils.executor import active_executor

STACK = ("traefik", "vector", "telegraf", "cloudflared")


class FakeTailscale:
  worker_tag = "worker"

  def __init__(self):
    self.listed = []          # (hostname, ip)
    self.answering = set()    # hostnames that answer a ping
    self.asked = None
    self.commands = []
    self.up_output = ["Container traefik  Running"]
    self.up_error = None

  def get_by_tag(self, tag):
    return [SimpleNamespace(hostname=h, ip=ip) for h, ip in self.listed]

  async def online_states(self, peers):
    self.asked = peers
    return {hostname: hostname in self.answering for hostname in peers}

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append((hostname, command))
    if self.up_error:
      raise self.up_error
    return 0, self.up_output


class Harness(WorkersMixin):
  worker_home_dir = "/opt/sage"

  def __init__(self):
    self.tailscale = FakeTailscale()
    self.stack_repairs = {}
    self.notices = []
    self.setups = []
    self.offlined = []
    self.failing = set()

  def notify(self, message, level="info"):
    self.notices.append((level, message))

  def refresh_worker_summary(self):
    pass

  async def setup_worker(self, worker, force=False):
    if worker.hostname in self.failing:
      raise Exception("boom")
    self.setups.append((worker.hostname, force))

  def set_worker_offline(self, worker):
    self.offlined.append(worker.hostname)


@pytest.fixture
def harness(db, clock):
  executor = ThreadPoolExecutor(max_workers=1)
  token = active_executor.set(executor)
  Reports()  # the Manager creates it at startup, which is when its silence starts
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
    harness.tailscale.listed = [("w1", "100.64.0.2"), ("w2", "100.64.0.2"), ("w3", "100.64.0.3")]
    harness.tailscale.answering = {"w2", "w3"}
    self.sync(harness)
    assert harness.offlined == ["w1"]
    assert harness.setups == [("w2", False), ("w3", False)]

  def test_a_worker_that_does_not_answer_is_taken_out(self, harness):
    add("w1", True)
    harness.tailscale.listed = [("w1", "100.64.0.2")]
    self.sync(harness)
    assert harness.offlined == ["w1"]

  def test_the_pings_are_given_what_the_database_knows_about_each_worker(self, harness):
    add("w1", True, "100.64.0.2")
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
    Reports().accept("w1", snapshot(running=("vector", "telegraf", "cloudflared")))
    harness.tailscale.up_output = ["Container traefik  Starting", "Container traefik  Started"]
    self.repair(harness)
    host, command = harness.tailscale.commands[0]
    assert host == "w1" and "up -d" in command and all(service in command for service in STACK)
    assert harness.notices == [("warning", "Worker w1: traefik not running; started its stack.")]

  def test_a_healthy_stack_is_left_alone(self, harness):
    add("w1", True)
    Reports().accept("w1", snapshot())
    self.repair(harness)
    assert harness.tailscale.commands == []

  def test_a_worker_out_of_the_pool_is_left_alone(self, harness):
    add("w1", False)
    Reports().accept("w1", snapshot(running=()))
    self.repair(harness)
    assert harness.tailscale.commands == []

  def test_a_worker_is_tried_at_most_once_per_cooldown(self, harness, clock):
    add("w1", True)
    harness.tailscale.up_output = ["Container traefik  Started"]
    down = snapshot(traefik_reachable=False)
    Reports().accept("w1", down)
    self.repair(harness)
    clock.advance(timedelta(seconds=config.STACK_REPAIR_COOLDOWN_SECONDS - 1))
    Reports().accept("w1", down)
    self.repair(harness)
    assert len(harness.tailscale.commands) == 1
    clock.advance(timedelta(seconds=1))
    Reports().accept("w1", down)
    self.repair(harness)
    assert len(harness.tailscale.commands) == 2

  def test_starting_that_changes_nothing_is_not_announced(self, harness):
    add("w1", True)
    Reports().accept("w1", snapshot(traefik_reachable=False))
    harness.tailscale.up_output = ["Container traefik  Running"]
    self.repair(harness)
    assert harness.notices == []

  def test_a_start_that_fails_is_reported_and_not_retried_at_once(self, harness):
    add("w1", True)
    Reports().accept("w1", snapshot(traefik_reachable=False))
    harness.tailscale.up_error = Exception("compose failed")
    self.repair(harness)
    self.repair(harness)
    assert harness.notices == [("error", "Worker w1: Traefik is not answering; starting its stack failed: compose failed")]
    assert len(harness.tailscale.commands) == 1

  def test_a_worker_that_never_reports_is_started_after_the_silence(self, harness, clock):
    add("w1", True)
    clock.advance(timedelta(seconds=config.STACK_SILENCE_SECONDS))
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
