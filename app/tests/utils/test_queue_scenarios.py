"""The real TaskQueue under simulated load: deploys, stops, status syncs, worker syncs, backups, restores,
pauses and shutdowns of simulated applications, with random delays and failures. A task body is a coroutine
or a function that waits, fails, or blocks in its lane's thread; the checks are the queue's promises:
conflicting scopes never overlap, nothing is lost or run twice, and every task is recorded once."""
import asyncio
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils.executor import active_executor, run_in_executor_with_context
from utils.logging import TaskFailed, task_id
from utils.queue import OnConflict, QueueState, TaskQueue

# Worker counts of the lanes in utils.executor.LANES.
POOLS = {"platform": 1, "common": 1, "app": 6, "metrics": 2}
GATE = object()    # a blocking call that ends when the test opens the thread gate


def conflict(first, second):
  """Two sets of scopes conflict when a scope of one equals, or is the parent of, a scope of the other."""
  return any(a == b or a.startswith(b + ":") or b.startswith(a + ":") for a in first for b in second)


def covers(held, wanted):
  """Some scope in `held` equals, or is the parent of, one in `wanted`."""
  return any(w == h or w.startswith(h + ":") for h in held for w in wanted)


def lane_of(scopes):
  roots = {scope.split(":")[0] for scope in scopes}
  return "platform" if "platform" in roots else sorted(roots)[0]


class Job:
  """One task of the simulation. `hold` waits for `release()`, `delay` sleeps, `block` blocks in a thread of the
  lane (for a sync job the job itself runs there), `fail` raises, `during` runs just before the end."""

  def __init__(self, sim, label, name, scopes, *, on_conflict, quiet, restore=False, hold=False, delay=0.0,
               block=None, fail=None, sync=False, during=None):
    self.sim = sim
    self.label = label
    self.name = name
    self.scopes = frozenset(scopes)
    self.on_conflict = on_conflict
    self.quiet = quiet
    self.restore = restore
    self.hold = hold
    self.delay = delay
    self.block = block
    self.fail = fail
    self.during = during
    self.lane = lane_of(self.scopes)
    self.gate = asyncio.Event()
    self.runs = 0
    self.admitted = None
    self.task = self.run_sync if sync else self.run

  def release(self):
    self.gate.set()

  def raise_if_failing(self):
    if self.fail == "task":
      raise TaskFailed()
    if self.fail == "error":
      raise RuntimeError(f"{self.label} broke")

  async def run(self, label=None):
    self.sim.begin(self)
    try:
      if self.hold:
        await self.gate.wait()
      if self.delay:
        await asyncio.sleep(self.delay)
      if self.block is not None:
        await run_in_executor_with_context(self.sim.block_in_thread, self.lane, self.block)
      if self.during:
        self.during(self)
      self.raise_if_failing()
    finally:
      self.sim.finish(self)

  def run_sync(self, label=None):
    self.sim.begin(self)
    try:
      if self.block is not None:
        self.sim.block_in_thread(self.lane, self.block)
      if self.during:
        self.during(self)
      self.raise_if_failing()
    finally:
      self.sim.finish(self)


class Sim:
  def __init__(self):
    self.pools = {root: ThreadPoolExecutor(max_workers=size, thread_name_prefix=f"sim-{root}")
                  for root, size in POOLS.items()}
    self.queue = TaskQueue(frozenset(self.pools), self.pools, record=self.record)
    self.lock = threading.Lock()
    self.thread_gate = threading.Event()
    self.jobs = {}
    self.recorded = []          # (label, status) in the order recorded
    self.trace = []             # ("start" | "end", label) in the order the bodies ran
    self.active = {}            # label -> scopes of the bodies running now
    self.violations = []
    self.concurrent_max = 0
    self.in_thread = dict.fromkeys(POOLS, 0)
    self.max_in_thread = dict.fromkeys(POOLS, 0)
    self.verify_admission = True

  # --- what the bodies report

  def begin(self, job):
    with self.lock:
      for other, scopes in self.active.items():
        if conflict(job.scopes, scopes):
          self.violations.append(f"{job.label} started while {other} was running")
      self.active[job.label] = job.scopes
      self.concurrent_max = max(self.concurrent_max, len(self.active))
      job.runs += 1
      self.trace.append(("start", job.label))

  def finish(self, job):
    with self.lock:
      self.active.pop(job.label, None)
      self.trace.append(("end", job.label))

  def block_in_thread(self, lane, seconds):
    with self.lock:
      self.in_thread[lane] += 1
      self.max_in_thread[lane] = max(self.max_in_thread[lane], self.in_thread[lane])
    try:
      if seconds is GATE:
        self.thread_gate.wait(timeout=10)
      else:
        time.sleep(seconds)
    finally:
      with self.lock:
        self.in_thread[lane] -= 1

  def record(self, task, status):
    with self.lock:
      self.recorded.append((task.params["label"], status))

  # --- driving it

  def add(self, label, *scopes, name=None, on_conflict=OnConflict.DEDUP, quiet=False, priority=False,
          restore=False, **options):
    name = name or label
    if restore and "during" not in options:
      options["during"] = lambda job: self.queue.end_restore(False)
    job = Job(self, label, name, scopes, on_conflict=on_conflict, quiet=quiet, restore=restore, **options)
    before = None
    if self.verify_admission:
      snapshot = self.queue.snapshot()
      before = (snapshot["running"], snapshot["queued"], self.queue.state)
    with self.lock:
      self.jobs[label] = job
    job.admitted = self.queue.add_task(
        task=job.task, scopes=set(scopes), executor=job.lane, name=name, params={"label": label}, task_id=label,
        on_conflict=on_conflict, quiet=quiet, priority=priority, restore=restore)
    if before:
      self.check_admission(job, before, priority)
    return job

  def check_admission(self, job, before, priority):
    """The queue's answer and its queue afterwards against a model written apart from the queue's own code."""
    running, queued, state = before

    def same(entry):
      return entry["name"] == job.name and set(entry["scopes"]) == set(job.scopes)

    expected_queue = [entry["task_id"] for entry in queued]
    admitted = True
    if job.restore and state != QueueState.RUNNING:
      admitted = False
    elif state == QueueState.STOPPING:
      admitted = False
    elif job.on_conflict == OnConflict.DEDUP and any(same(entry) for entry in running + queued):
      admitted = False
    if admitted:
      if job.on_conflict == OnConflict.REPLACE:
        queued = [entry for entry in queued if not same(entry)]
      expected_queue = [entry["task_id"] for entry in queued]
      position = len(queued)
      if priority:
        position = 0
        for entry in queued:
          if not covers(set(entry["scopes"]), set(job.scopes)):
            break
          position += 1
      expected_queue.insert(position, job.label)
    assert job.admitted == admitted, f"{job.label}: admitted={job.admitted}, the model says {admitted}"
    assert self.queued() == expected_queue, f"{job.label}: the queue is {self.queued()}, the model says {expected_queue}"

  def expected_starts(self):
    """What a dispatch tick starts, from the queue as it is now, by a model written apart from the queue's own."""
    snapshot = self.queue.snapshot()
    state = self.queue.state
    if state not in (QueueState.RUNNING, QueueState.RESTORING):
      return []
    held = [set(entry["scopes"]) for entry in snapshot["running"]]
    starts = []
    for entry in snapshot["queued"]:
      if state == QueueState.RESTORING and not self.jobs[entry["task_id"]].restore:
        continue
      scopes = set(entry["scopes"])
      if any(conflict(scopes, other) for other in held):
        continue
      starts.append(entry["task_id"])
      held.append(scopes)
    return starts

  def entries(self):
    snapshot = self.queue.snapshot()
    return snapshot["running"] + snapshot["queued"]

  def running(self):
    return [entry["params"]["label"] for entry in self.queue.snapshot()["running"]]

  def queued(self):
    return [entry["params"]["label"] for entry in self.queue.snapshot()["queued"]]

  def starts(self):
    return [label for kind, label in self.trace if kind == "start"]

  async def settle(self, rounds=10):
    for _ in range(rounds):
      self.queue.dispatch_tick()
      await asyncio.sleep(0)

  async def until(self, condition, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
      self.queue.dispatch_tick()
      if condition():
        return
      if time.monotonic() > deadline:
        raise AssertionError(f"timed out: running={self.running()} queued={self.queued()} state={self.queue.state}")
      await asyncio.sleep(0.002)

  async def drain(self, timeout=30):
    await self.until(lambda: not self.entries(), timeout)

  async def run_ticker(self, stop, every=0.001):
    """The scheduler's one-second dispatch tick, sped up, checking each tick against the model of dispatch."""
    while not stop.is_set():
      before = self.running()
      expected = self.expected_starts() if self.verify_admission else None
      self.queue.dispatch_tick()
      snapshot = self.queue.snapshot()
      after = [entry["params"]["label"] for entry in snapshot["running"]]
      if after[:len(before)] != before:
        self.violations.append(f"the running tasks {before} became {after}")
      if expected is not None and after[len(before):] != expected:
        self.violations.append(f"a tick started {after[len(before):]}, the model says {expected}")
      self.check_snapshot(snapshot)
      await asyncio.sleep(every)

  def check_snapshot(self, snapshot):
    running = snapshot["running"]
    for index, first in enumerate(running):
      for second in running[index + 1:]:
        if conflict(first["scopes"], second["scopes"]):
          self.violations.append(f"{first['task_id']} and {second['task_id']} are both running")
    labels = [entry["task_id"] for entry in running + snapshot["queued"]]
    if len(labels) != len(set(labels)):
      self.violations.append(f"a task is listed twice: {labels}")

  def verify(self, *, cancelled_by_restore=False):
    """Every promise at once, once the queue is idle."""
    assert self.violations == []
    assert self.queue.snapshot() == {"running": [], "queued": []}
    assert self.queue.state == QueueState.RUNNING
    assert self.active == {}
    statuses = {}
    for label, status in self.recorded:
      statuses.setdefault(label, []).append(status)
    assert set(statuses) <= set(self.jobs), "a task nobody added was recorded"
    for label, job in self.jobs.items():
      recorded = statuses.get(label, [])
      assert job.runs <= 1, f"{label} ran {job.runs} times"
      if not job.admitted:
        assert job.runs == 0 and recorded == [], f"{label} was refused but left a trace"
      elif job.runs == 1:
        expected = ["failed"] if job.fail else ([] if job.quiet else ["completed"])
        assert recorded == expected, f"{label}: recorded {recorded}, expected {expected}"
      else:
        assert self.superseded_later(job) or cancelled_by_restore, f"{label} was admitted and never ran"
        assert recorded == ([] if job.quiet else ["cancelled"]), f"{label}: recorded {recorded} for a cancelled task"

  def superseded_later(self, job):
    """A pending task is replaced by a later REPLACE of the same name and scopes, whatever admitted it."""
    return any(other.on_conflict == OnConflict.REPLACE and other.admitted and other is not job
               and other.name == job.name and other.scopes == job.scopes for other in self.jobs.values())

  async def close(self):
    self.thread_gate.set()
    for job in list(self.jobs.values()):
      job.release()
    self.queue.resume()
    try:
      await self.drain(timeout=5)
    except AssertionError:
      pass
    for pool in self.pools.values():
      pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def eager_threads():
  """Threads switch as often as they can, which is what exposes a missing lock."""
  interval = sys.getswitchinterval()
  sys.setswitchinterval(1e-6)
  yield
  sys.setswitchinterval(interval)


@pytest.fixture
async def sim():
  sim = Sim()
  yield sim
  await sim.close()


# --- deploys

class TestDeploys:
  async def test_applications_deploy_in_parallel(self, sim):
    jobs = [sim.add(f"deploy#{i}", f"app:p-a{i}", name="deploy", hold=True) for i in range(10)]
    await sim.settle()
    assert len(sim.running()) == 10 and sim.queued() == []
    for job in jobs:
      job.release()
    await sim.drain()
    sim.verify()
    assert sim.concurrent_max == 10

  async def test_operations_on_one_application_run_one_at_a_time_in_the_order_added(self, sim):
    jobs = [sim.add(f"op#{i}", "app:p-a", name="op", on_conflict=OnConflict.QUEUE, hold=True) for i in range(6)]
    for job in jobs:
      await sim.settle()
      assert sim.running() == [job.label]
      job.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == [job.label for job in jobs]

  async def test_a_second_deploy_is_refused_while_the_first_runs_and_accepted_after(self, sim):
    first = sim.add("deploy#1", "app:p-a", name="deploy", hold=True)
    await sim.settle()
    assert sim.add("deploy#2", "app:p-a", name="deploy").admitted is False
    first.release()
    await sim.drain()
    assert sim.add("deploy#3", "app:p-a", name="deploy").admitted is True
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["deploy#1", "deploy#3"]

  async def test_a_second_deploy_is_refused_while_the_first_is_waiting_behind_another_operation(self, sim):
    restart = sim.add("restart", "app:p-a", hold=True)
    await sim.settle()
    assert sim.add("deploy#1", "app:p-a", name="deploy").admitted is True
    assert sim.add("deploy#2", "app:p-a", name="deploy").admitted is False
    restart.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["restart", "deploy#1"]

  async def test_a_deploy_of_a_parent_scope_waits_for_every_application_and_holds_them_back(self, sim):
    a = sim.add("deploy-a", "app:p-a", hold=True)
    b = sim.add("deploy-b", "app:p-b", hold=True)
    await sim.settle()
    everything = sim.add("everything", "app", hold=True)
    await sim.settle()
    assert sim.queued() == ["everything"]
    a.release()
    await sim.settle()
    assert sim.queued() == ["everything"]
    b.release()
    await sim.settle()
    assert sim.running() == ["everything"]
    late = sim.add("deploy-c", "app:p-c")
    await sim.settle()
    assert sim.queued() == ["deploy-c"]
    everything.release()
    await sim.drain()
    sim.verify()
    assert sim.starts().index("deploy-c") > sim.starts().index("everything")
    assert late.runs == 1

  async def test_a_failing_deploy_frees_its_application_and_leaves_the_others_alone(self, sim):
    sim.add("deploy-a#1", "app:p-a", name="deploy-a1", fail="error")
    sim.add("deploy-a#2", "app:p-a", name="deploy-a2", on_conflict=OnConflict.QUEUE)
    sim.add("deploy-b", "app:p-b", fail="task")
    sim.add("deploy-c", "app:p-c")
    await sim.drain()
    sim.verify()
    assert sorted(sim.recorded) == [("deploy-a#1", "failed"), ("deploy-a#2", "completed"),
                                    ("deploy-b", "failed"), ("deploy-c", "completed")]
    assert sim.starts().index("deploy-a#2") > sim.trace.index(("end", "deploy-a#1"))

  async def test_blocking_work_of_many_deploys_is_limited_by_the_size_of_the_lane(self, sim):
    for index in range(10):
      sim.add(f"deploy#{index}", f"app:p-a{index}", name="deploy", block=GATE)
    await sim.until(lambda: sim.in_thread["app"] == POOLS["app"])
    await asyncio.sleep(0.05)
    assert sim.in_thread["app"] == POOLS["app"]
    assert len(sim.running()) == 10
    sim.thread_gate.set()
    await sim.drain()
    sim.verify()
    assert sim.max_in_thread["app"] == POOLS["app"]

  async def test_a_task_that_adds_another_does_not_wait_for_it(self, sim):
    def add_children(job):
      sim.add("child-same", "app:p-a", name="child-same", on_conflict=OnConflict.QUEUE)
      sim.add("child-other", "app:p-b", name="child-other")

    parent = sim.add("parent", "app:p-a", hold=True, during=add_children)
    await sim.settle()
    parent.release()
    await sim.drain()
    sim.verify()
    assert sim.trace.index(("end", "parent")) < sim.trace.index(("start", "child-same"))


# --- status syncs

class TestStatusSyncs:
  def sync(self, sim, label, app, **options):
    return sim.add(label, f"app:p-{app}", name=f"sync-{app}", on_conflict=OnConflict.REPLACE, quiet=True, **options)

  async def test_syncs_requested_during_a_deploy_become_one_that_runs_after_it(self, sim):
    deploy = sim.add("deploy", "app:p-a", hold=True)
    await sim.settle()
    syncs = [self.sync(sim, f"sync#{i}", "a") for i in range(5)]
    assert sim.queued() == ["sync#4"]
    assert all(sync.admitted for sync in syncs)
    deploy.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["deploy", "sync#4"]

  async def test_a_request_while_a_sync_runs_keeps_exactly_one_more_run(self, sim):
    first = self.sync(sim, "sync#1", "a", hold=True)
    await sim.settle()
    self.sync(sim, "sync#2", "a")
    self.sync(sim, "sync#3", "a")
    assert sim.running() == ["sync#1"] and sim.queued() == ["sync#3"]
    first.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["sync#1", "sync#3"]

  async def test_the_syncs_of_different_applications_do_not_replace_each_other(self, sim):
    sim.add("deploy-a", "app:p-a", hold=True)
    sim.add("deploy-b", "app:p-b", hold=True)
    await sim.settle()
    for _ in range(3):
      self.sync(sim, f"sync-a#{_}", "a")
      self.sync(sim, f"sync-b#{_}", "b")
    assert sorted(sim.queued()) == ["sync-a#2", "sync-b#2"]

  async def test_a_deploy_holds_up_only_its_own_application(self, sim):
    sim.add("deploy-a", "app:p-a", hold=True)
    await sim.settle()
    self.sync(sim, "sync-a", "a")
    self.sync(sim, "sync-b", "b")
    await sim.until(lambda: "sync-b" in sim.starts())
    assert sim.queued() == ["sync-a"]

  async def test_a_failing_sync_is_recorded_and_the_next_request_still_runs(self, sim):
    self.sync(sim, "sync#1", "a", fail="error")
    await sim.drain()
    self.sync(sim, "sync#2", "a")
    await sim.drain()
    sim.verify()
    assert sim.recorded == [("sync#1", "failed")]
    assert sim.starts() == ["sync#1", "sync#2"]

  async def test_a_flood_of_requests_never_leaves_more_than_one_waiting_per_application(self, sim):
    deploys = [sim.add(f"deploy-{app}", f"app:p-{app}", hold=True) for app in "abcdef"]
    await sim.settle()
    for request in range(120):
      for app in "abcdef":
        self.sync(sim, f"sync-{app}#{request}", app)
      assert len(sim.queued()) == 6
    for deploy in deploys:
      deploy.release()
    await sim.drain()
    sim.verify()
    assert sorted(label for label in sim.starts() if label.startswith("sync")) == [f"sync-{app}#119" for app in "abcdef"]


# --- platform work

class TestPlatformWork:
  def backup(self, sim, label, **options):
    return sim.add(label, "platform", "app", name="backup_database", on_conflict=OnConflict.REPLACE, **options)

  async def test_a_backup_waits_for_running_applications_and_later_deploys_start_past_it(self, sim):
    a = sim.add("deploy-a", "app:p-a", hold=True)
    await sim.settle()
    backup = self.backup(sim, "backup", hold=True)
    await sim.settle()
    assert sim.queued() == ["backup"]
    b = sim.add("deploy-b", "app:p-b", hold=True)
    await sim.settle()
    assert sorted(sim.running()) == ["deploy-a", "deploy-b"] and sim.queued() == ["backup"]
    a.release()
    await sim.settle()
    assert sim.queued() == ["backup"]
    b.release()
    await sim.settle()
    assert sim.running() == ["backup"]
    sim.add("deploy-c", "app:p-c")
    await sim.settle()
    assert sim.queued() == ["deploy-c"]
    backup.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["deploy-a", "deploy-b", "backup", "deploy-c"]

  async def test_a_backup_keeps_waiting_while_applications_always_overlap_and_starts_at_the_first_gap(self, sim):
    previous = sim.add("overlap#0", "app:p-a", hold=True)
    await sim.settle()
    waiting = self.backup(sim, "backup")
    for round_ in range(1, 15):
      following = sim.add(f"overlap#{round_}", f"app:p-{'abc'[round_ % 3]}", hold=True)
      await sim.settle()
      previous.release()
      await sim.settle()
      assert waiting.runs == 0, "the backup started while an application was running"
      previous = following
    previous.release()
    await sim.drain()
    sim.verify()
    assert waiting.runs == 1

  async def test_platform_tasks_never_overlap_and_keep_their_order(self, sim):
    jobs = [sim.add(f"platform#{i}", "platform", name=f"op{i}", on_conflict=OnConflict.QUEUE, delay=0.003)
            for i in range(8)]
    await sim.drain()
    sim.verify()
    assert sim.starts() == [job.label for job in jobs]

  async def test_a_worker_sync_that_is_still_running_is_not_started_twice(self, sim):
    first = sim.add("sync_workers#1", "platform", name="sync_workers", quiet=True, hold=True)
    await sim.settle()
    for index in range(2, 6):
      assert sim.add(f"sync_workers#{index}", "platform", name="sync_workers", quiet=True).admitted is False
    first.release()
    await sim.drain()
    sim.verify()

  async def test_a_priority_task_goes_behind_the_platform_work_and_ahead_of_the_applications(self, sim):
    running = self.backup(sim, "backup", hold=True)
    await sim.settle()
    sim.add("cleanup", "platform", name="cleanup")
    sim.add("sync-a", "app:p-a", quiet=True)
    sim.add("sync-b", "app:p-b", quiet=True)
    sim.add("urgent", "platform", name="urgent", priority=True)
    assert sim.queued() == ["cleanup", "urgent", "sync-a", "sync-b"]
    running.release()
    await sim.drain()
    sim.verify()
    assert sim.starts().index("cleanup") < sim.starts().index("urgent")

  async def test_common_and_metrics_work_goes_on_beside_a_backup(self, sim):
    backup = self.backup(sim, "backup", hold=True)
    await sim.settle()
    sim.add("check", "common:s3", quiet=True, hold=True)
    sim.add("collect", "metrics:manager", quiet=True, hold=True)
    await sim.settle()
    assert sorted(sim.running()) == ["backup", "check", "collect"]
    backup.release()
    sim.jobs["check"].release()
    sim.jobs["collect"].release()
    await sim.drain()
    sim.verify()


# --- restore

class TestRestore:
  async def run_restore(self, sim, restored):
    follow = Job(sim, "follow-up", "follow-up", {"app:p-a"}, on_conflict=OnConflict.DEDUP, quiet=False)
    sim.jobs["follow-up"] = follow

    def end(job):
      sim.queue.end_restore(restored, follow_ups=[dict(
          task=follow.task, scopes={"app:p-a"}, executor="app", name="follow-up", params={"label": "follow-up"},
          task_id="follow-up")])
      follow.admitted = restored

    deploy = sim.add("deploy-a", "app:p-a", hold=True)
    await sim.settle()
    sim.add("sync-a", "app:p-a", name="sync-a", on_conflict=OnConflict.REPLACE, quiet=True)
    sim.add("backup", "platform", "app", name="backup_database", on_conflict=OnConflict.REPLACE)
    restore = sim.add("restore", "platform", "app", name="restore", restore=True, hold=True, during=end)
    assert sim.queue.state == QueueState.RESTORING
    late = sim.add("deploy-b", "app:p-b")
    assert sim.add("restore#2", "platform", "app", name="restore", restore=True).admitted is False
    await sim.settle()
    assert sim.running() == ["deploy-a"], "nothing else may start while restoring"
    deploy.release()
    await sim.settle()
    assert sim.running() == ["restore"]
    assert sorted(sim.queued()) == ["backup", "deploy-b", "sync-a"]
    restore.release()
    await sim.drain()
    return late

  async def test_a_restore_that_took_effect_cancels_what_was_waiting_and_runs_the_follow_up(self, sim):
    late = await self.run_restore(sim, restored=True)
    sim.verify(cancelled_by_restore=True)
    assert sim.starts() == ["deploy-a", "restore", "follow-up"]
    assert late.runs == 0
    assert sorted(sim.recorded) == [("backup", "cancelled"), ("deploy-a", "completed"), ("deploy-b", "cancelled"),
                                    ("follow-up", "completed"), ("restore", "completed")]

  async def test_a_restore_that_failed_lets_what_was_waiting_run(self, sim):
    late = await self.run_restore(sim, restored=False)
    sim.verify()
    assert late.runs == 1
    starts = sim.starts()
    assert starts[:2] == ["deploy-a", "restore"]
    assert starts.index("backup") > max(starts.index("sync-a"), starts.index("deploy-b"))
    assert "follow-up" not in starts

  async def test_the_queue_is_usable_again_after_a_restore(self, sim):
    await self.run_restore(sim, restored=True)
    assert sim.add("deploy-c", "app:p-c").admitted is True
    await sim.drain()
    sim.verify(cancelled_by_restore=True)


# --- pause and stop

class TestPauseAndStop:
  async def test_a_recurring_task_added_for_a_whole_pause_runs_once_after_it(self, sim):
    sim.queue.pause()
    admitted = [sim.add(f"sync_workers#{i}", "platform", name="sync_workers", quiet=True).admitted for i in range(100)]
    assert admitted.count(True) == 1
    await sim.settle()
    assert sim.running() == [] and sim.queued() == ["sync_workers#0"]
    sim.queue.resume()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["sync_workers#0"]

  async def test_running_tasks_finish_during_a_pause_and_nothing_new_starts(self, sim):
    running = sim.add("deploy-a", "app:p-a", hold=True)
    await sim.settle()
    sim.queue.pause()
    waiting = sim.add("deploy-b", "app:p-b")
    running.release()
    await sim.settle()
    assert sim.running() == [] and waiting.runs == 0
    assert ("deploy-a", "completed") in sim.recorded
    sim.queue.resume()
    await sim.drain()
    sim.verify()

  async def test_stopping_cancels_the_waiting_lets_the_running_finish_and_refuses_what_they_add(self, sim):
    parent = sim.add("parent", "app:p-a", hold=True,
                     during=lambda job: sim.add("child", "app:p-a", name="child", on_conflict=OnConflict.QUEUE))
    await sim.settle()
    sim.add("waiting", "app:p-a", name="waiting", on_conflict=OnConflict.QUEUE)
    sim.queue.stop()
    assert sim.recorded == [("waiting", "cancelled")]
    parent.release()
    await sim.until(lambda: not sim.running())
    assert sorted(sim.recorded) == [("child", "cancelled"), ("parent", "completed"), ("waiting", "cancelled")]
    assert sim.queue.state == QueueState.STOPPING
    assert sim.violations == []


class TestShutdownOfTheLoop:
  async def test_running_tasks_cancelled_by_the_loop_are_recorded_failed_and_free_their_scopes(self, sim):
    jobs = [sim.add(f"deploy#{index}", f"app:p-a{index}", hold=True) for index in range(5)]
    sim.add("queued-behind", "app:p-a0", name="queued-behind", on_conflict=OnConflict.QUEUE)
    await sim.settle()
    assert len(sim.running()) == 5
    handles = list(sim.queue._tasks)
    for handle in handles:
      handle.cancel()
    await asyncio.gather(*handles, return_exceptions=True)
    assert sorted(label for label, status in sim.recorded if status == "failed") == sorted(job.label for job in jobs)
    assert sim.running() == []
    sim.queue.dispatch_tick()
    await sim.settle()
    assert "queued-behind" in sim.starts(), "the cancelled tasks kept their scopes"


# --- lanes

class TestLanes:
  @pytest.mark.parametrize("sync", [False, True], ids=["offloaded from a coroutine", "a function"])
  @pytest.mark.parametrize("lane, scopes, size", [
      ("common", [f"common:{key}" for key in "abcdef"], 1),
      ("metrics", [f"metrics:h{index}" for index in range(8)], 2),
      ("app", [f"app:p-a{index}" for index in range(12)], 6),
  ])
  async def test_blocking_work_never_exceeds_the_lane_however_many_scopes_are_free(self, sim, sync, lane, scopes, size):
    for index, scope in enumerate(scopes):
      sim.add(f"job#{index}", scope, name="job", block=0.03, sync=sync)
    await sim.drain()
    sim.verify()
    assert sim.max_in_thread[lane] == size

  async def test_coroutines_are_not_limited_by_the_size_of_their_lane(self, sim):
    jobs = [sim.add(f"job#{index}", f"common:k{index}", hold=True) for index in range(40)]
    await sim.settle()
    assert len(sim.running()) == 40
    for job in jobs:
      job.release()
    await sim.drain()
    sim.verify()

  async def test_each_task_runs_with_its_own_id_and_lane_however_they_interleave(self, sim):
    seen = {}

    def look(job):
      seen[job.label] = (task_id.get(), active_executor.get())

    rng = random.Random(7)
    scopes = ["app:p-a", "app:p-b", "common:x", "metrics:h", "platform"]
    for index in range(60):
      scope = rng.choice(scopes)
      sim.add(f"job#{index}", scope, name="job", on_conflict=OnConflict.QUEUE, delay=rng.uniform(0, 0.01),
              block=rng.choice([None, 0.001]), during=look)
    await sim.drain()
    sim.verify()
    assert len(seen) == 60
    for label, (current, pool) in seen.items():
      assert current == label
      assert pool is sim.pools[sim.jobs[label].lane]


# --- brute force

APPS = [f"p-a{index}" for index in range(8)]


def random_operation(rng, index):
  """One operation the way the manager adds it: (label, scopes, options)."""
  app = f"app:{rng.choice(APPS)}"
  kind = rng.choices(
      ["deploy", "stop", "sync", "workers", "reconcile", "backup", "check", "metrics", "s3", "everything"],
      weights=[22, 8, 28, 6, 5, 3, 5, 6, 6, 1])[0]
  options = dict(delay=rng.uniform(0, 0.012), fail=rng.choices([None, "task", "error"], [90, 5, 5])[0],
                 block=rng.choice([None, None, rng.uniform(0, 0.006)]), sync=rng.random() < 0.2)
  label = f"{kind}#{index}"
  if kind == "deploy":
    return label, [app], dict(name="deploy", priority=rng.random() < 0.1, **options)
  if kind == "stop":
    return label, [app], dict(name="stop", **options)
  if kind == "sync":
    return label, [app], dict(name=f"sync-{app}", on_conflict=OnConflict.REPLACE, quiet=True, **options)
  if kind == "workers":
    return label, ["platform"], dict(name="sync_workers", quiet=True, **options)
  if kind == "reconcile":
    return label, ["platform"], dict(name="reconcile", quiet=True, **options)
  if kind == "backup":
    return label, ["platform", "app"], dict(name="backup_database", on_conflict=OnConflict.REPLACE, **options)
  if kind == "check":
    return label, [f"common:{rng.choice('abc')}"], dict(name="check", on_conflict=OnConflict.REPLACE, quiet=True, **options)
  if kind == "metrics":
    return label, [f"metrics:h{rng.randrange(3)}"], dict(name="collect", quiet=True, **options)
  if kind == "s3":
    return label, ["common"], dict(name=f"s3-{index}", on_conflict=OnConflict.QUEUE, **options)
  return label, ["app"], dict(name="everything", **options)


SEEDS = range(12)

NAMES = ["deploy", "sync", "backup", "check"]
SCOPE_CHOICES = ["platform", "app", "app:p-a", "app:p-b", "app:p-c", "common", "common:x", "metrics:h1"]


class TestAdmission:
  """Every add against the model: accepted or refused, and the exact queue afterwards."""

  @pytest.mark.parametrize("seed", range(8))
  async def test_a_storm_of_admissions_while_nothing_runs(self, sim, seed):
    rng = random.Random(seed)
    sim.queue.pause()
    for index in range(600):
      scopes = rng.sample(SCOPE_CHOICES, rng.choice([1, 1, 1, 2]))
      sim.add(f"op#{index}", *scopes, name=rng.choice(NAMES), on_conflict=rng.choice(list(OnConflict)),
              quiet=rng.random() < 0.4, priority=rng.random() < 0.2, restore=rng.random() < 0.03)
    sim.queue.resume()
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()

  @pytest.mark.parametrize("seed", range(8))
  async def test_a_storm_of_admissions_while_tasks_start_and_end(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    for index in range(500):
      scopes = rng.sample(SCOPE_CHOICES, rng.choice([1, 1, 1, 2]))
      sim.add(f"op#{index}", *scopes, name=rng.choice(NAMES), on_conflict=rng.choice(list(OnConflict)),
              quiet=rng.random() < 0.4, priority=rng.random() < 0.2, delay=rng.uniform(0, 0.006),
              fail=rng.choice([None, None, None, "error"]))
      await asyncio.sleep(rng.uniform(0, 0.002))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()

  async def test_the_admissions_after_a_stop_are_all_refused(self, sim):
    sim.queue.stop()
    rng = random.Random(1)
    for index in range(50):
      job = sim.add(f"late#{index}", rng.choice(SCOPE_CHOICES), name=rng.choice(NAMES), on_conflict=rng.choice(list(OnConflict)))
      assert job.admitted is False
    assert sim.queued() == []
    assert sorted(label for label, status in sim.recorded if status == "cancelled") == sorted(
        f"late#{index}" for index in range(50) if not sim.jobs[f"late#{index}"].quiet)


class TestBruteForce:
  @pytest.mark.parametrize("seed", SEEDS)
  async def test_deploys_syncs_and_platform_work_crossing_at_random(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    for index in range(140):
      label, scopes, options = random_operation(rng, index)
      sim.add(label, *scopes, **options)
      await asyncio.sleep(rng.uniform(0, 0.004))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert sim.concurrent_max >= 3, "the workload never ran in parallel"
    for lane, size in POOLS.items():
      assert sim.max_in_thread[lane] <= size

  @pytest.mark.parametrize("policy", [OnConflict.DEDUP, OnConflict.REPLACE])
  async def test_threads_racing_to_add_the_same_task_leave_exactly_one(self, sim, eager_threads, policy):
    sim.verify_admission = False
    sim.queue.pause()
    threads, rounds = 8, 150
    barrier = threading.Barrier(threads)

    def race(number):
      for round_ in range(rounds):
        barrier.wait()
        sim.add(f"round{round_}-t{number}", f"app:p-a{round_ % 5}", name=f"race{round_}", on_conflict=policy)

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=threads) as producers:
      await asyncio.gather(*(loop.run_in_executor(producers, race, number) for number in range(threads)))
    pending = sim.queued()
    assert len(pending) == rounds, f"{len(pending)} pending for {rounds} different tasks"
    assert len({label.split("-")[0] for label in pending}) == rounds
    admitted = sum(job.admitted for job in sim.jobs.values())
    assert admitted == (rounds if policy == OnConflict.DEDUP else rounds * threads)
    sim.queue.resume()
    await sim.drain()
    sim.verify()

  @pytest.mark.parametrize("seed", range(4))
  async def test_threads_adding_while_the_loop_dispatches(self, sim, eager_threads, seed):
    sim.verify_admission = False
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    threads, per_thread = 6, 50

    def produce(number):
      rng = random.Random(seed * 100 + number)
      for index in range(per_thread):
        label, scopes, options = random_operation(rng, number * per_thread + index)
        sim.add(f"t{number}-{label}", *scopes, **options)
        time.sleep(rng.uniform(0, 0.002))

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=threads) as producers:
      await asyncio.gather(*(loop.run_in_executor(producers, produce, number) for number in range(threads)))
    await sim.drain()
    stop.set()
    await ticker
    assert len(sim.jobs) == threads * per_thread
    sim.verify()

  @pytest.mark.parametrize("seed", range(6))
  async def test_operations_on_one_application_keep_the_order_they_were_added_in(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    added = {app: [] for app in APPS[:4]}
    for index in range(120):
      app = rng.choice(list(added))
      label = f"op#{index}"
      sim.add(label, f"app:{app}", name=label, on_conflict=OnConflict.QUEUE, delay=rng.uniform(0, 0.008),
              fail=rng.choice([None, None, None, "error"]))
      added[app].append(label)
      await asyncio.sleep(rng.uniform(0, 0.002))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    starts = sim.starts()
    for app, labels in added.items():
      assert [label for label in starts if label in set(labels)] == labels

  @pytest.mark.parametrize("restored", [True, False])
  @pytest.mark.parametrize("seed", range(6))
  async def test_a_restore_in_the_middle_of_the_storm(self, sim, seed, restored):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    others_started_during = []

    def end(job):
      during = [label for kind, label in sim.trace[sim.trace.index(("start", "restore")) + 1:] if kind == "start"]
      others_started_during.extend(during)
      sim.queue.end_restore(restored)

    restore_at = rng.randrange(40, 100)
    for index in range(140):
      label, scopes, options = random_operation(rng, index)
      sim.add(label, *scopes, **options)
      if index == restore_at:
        sim.add("restore", "platform", "app", name="restore", restore=True, delay=0.02, during=end)
      await asyncio.sleep(rng.uniform(0, 0.003))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify(cancelled_by_restore=restored)
    assert sim.jobs["restore"].runs == 1
    assert others_started_during == [], "something started while the restore ran"

  @pytest.mark.parametrize("seed", range(6))
  async def test_pausing_and_resuming_at_random(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    for index in range(140):
      label, scopes, options = random_operation(rng, index)
      sim.add(label, *scopes, **options)
      if rng.random() < 0.08:
        sim.queue.pause()
      elif rng.random() < 0.25:
        sim.queue.resume()
      await asyncio.sleep(rng.uniform(0, 0.003))
    sim.queue.resume()
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()

  @pytest.mark.parametrize("seed", range(4))
  async def test_syncs_cannot_pile_up_behind_deploys_that_cross(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    waiting_most = 0
    for index in range(200):
      app = rng.choice(APPS)
      if rng.random() < 0.15:
        sim.add(f"deploy#{index}", f"app:{app}", name="deploy", delay=rng.uniform(0.01, 0.04))
      sim.add(f"sync#{index}", f"app:{app}", name=f"sync-{app}", on_conflict=OnConflict.REPLACE, quiet=True,
              delay=rng.uniform(0, 0.006))
      pending = [entry for entry in sim.queue.snapshot()["queued"] if entry["name"].startswith("sync-")]
      assert len(pending) <= len(APPS), "more than one sync waits for an application"
      waiting_most = max(waiting_most, len(pending))
      await asyncio.sleep(rng.uniform(0, 0.003))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert waiting_most >= 1, "the syncs never had to wait"
