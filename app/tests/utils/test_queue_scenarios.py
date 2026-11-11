"""The real TaskQueue under simulated load: container deploys, stops and reconciliations, host-wide worker changes,
domain syncs, certificate work, backups, restores, pauses and shutdowns, with random delays and failures. A task body
is a coroutine or a function that waits, fails, or blocks in its lane's thread; the checks are the queue's promises:
conflicting scopes never overlap, each request's outcome and the queue afterwards match a model written apart from the
queue, nothing is lost or run twice, and every task is recorded once, visible when a visible request asked for it."""
import asyncio
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils.executor import active_executor, run_in_executor_with_context
from utils.logging import TaskFailed, task_id
from utils.queue import OnConflict, Outcome, QueueState, TaskQueue

# Worker counts of the lanes in utils.executor.LANES.
POOLS = {"platform": 1, "common": 1, "app": 6, "metrics": 2}
ROOTS = frozenset({"worker", "domain", "certificate", "backup", "discovery", "settings", "release", "metrics"})
LANE_OF_ROOT = {"certificate": "platform", "backup": "platform", "settings": "common",
                "release": "common", "metrics": "metrics"}
GATE = object()    # a blocking call that ends when the test opens the thread gate


def conflict(first, second):
  """Two sets of scopes conflict when a scope of one equals, or is the parent of, a scope of the other."""
  return any(a == b or a.startswith(b + ":") or b.startswith(a + ":") for a in first for b in second)


def covers(held, wanted):
  """Some scope in `held` equals, or is the parent of, one in `wanted`."""
  return any(w == h or w.startswith(h + ":") for h in held for w in wanted)


def lane_of(scopes):
  roots = sorted({scope.split(":")[0] for scope in scopes})
  return next((LANE_OF_ROOT[root] for root in roots if root in LANE_OF_ROOT), "app")


def container(index, workers=3):
  return f"worker:w{index % workers}:container:c{index}"


def lifecycle(sim, label, index, name, **options):
  """A user's Deploy or Stop: one lifecycle slot per container."""
  return sim.add(label, container(index), key=("lifecycle", f"c{index}"), name=name, on_conflict=OnConflict.REVISE,
                 **options)


class Job:
  """One request of the simulation. `hold` waits for `release()`, `delay` sleeps, `block` blocks in a thread of the
  lane (for a sync job the job itself runs there), `fail` raises, `during` runs just before the end. A `restore` is the platform restore, on the platform lane."""

  def __init__(self, sim, label, key, scopes, *, on_conflict, quiet, name=None, restore=False, hold=False, delay=0.0,
               block=None, fail=None, sync=False, during=None):
    self.sim = sim
    self.label = label
    self.key = key
    self.name = name or key[0]
    self.scopes = frozenset(scopes)
    self.on_conflict = on_conflict
    self.quiet = quiet
    self.restore = restore
    self.hold = hold
    self.delay = delay
    self.block = block
    self.fail = fail
    self.during = during
    self.lane = "platform" if restore else lane_of(self.scopes)
    self.gate = asyncio.Event()
    self.runs = 0
    self.added = None
    self.cancelled = False      # its pending task was cancelled by request
    self.quiet_now = None       # the model's word on whether its task is still quiet; None when not modelled
    self.task = self.run_sync if sync else self.run

  @property
  def outcome(self):
    return self.added.outcome

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
    self.pools = {lane: ThreadPoolExecutor(max_workers=size, thread_name_prefix=f"sim-{lane}")
                  for lane, size in POOLS.items()}
    self.notified = 0
    self.queue = TaskQueue(ROOTS, self.pools, record=self.record, notify=self.notify)
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
    self.seen_key = None
    self.seen_notified = 0

  # --- what the bodies report

  def begin(self, job):
    with self.lock:
      for other, scopes in self.active.items():
        if conflict(job.scopes, scopes) or job.restore or self.jobs[other].restore:
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

  def notify(self):
    self.notified += 1

  def record(self, task, status):
    with self.lock:
      self.recorded.append((task.params["label"], status))

  # --- driving it

  def add(self, label, *scopes, key=None, name=None, on_conflict=OnConflict.DEDUP, quiet=False, priority=False,
          restore=False, **options):
    key = key or (label,)
    if restore and "during" not in options:
      options["during"] = lambda job: self.queue.end_restore()
    job = Job(self, label, key, scopes, on_conflict=on_conflict, quiet=quiet, name=name, restore=restore, **options)
    before = None
    if self.verify_admission:
      snapshot = self.queue.snapshot()
      before = (snapshot["running"], snapshot["queued"], self.queue.state)
    with self.lock:
      self.jobs[label] = job
    if restore:
      job.added = self.queue.request_restore(task=job.task, name=job.name, executor=job.lane, params={"label": label},
                                             labels={"Job": label}, task_id=label)
    else:
      job.added = self.queue.add_task(
          task=job.task, key=key, name=name, scopes=set(scopes), executor=job.lane, params={"label": label},
          labels={"Job": label}, task_id=label, on_conflict=on_conflict, quiet=quiet, priority=priority)
    if before:
      self.check_admission(job, before, priority)
    return job

  def cancel(self, key=None, name=None, scope=None):
    """Cancel pending tasks, checked against the model: the pending ones with the key, the name and a scope at or below
    `scope` (each when given) go, except the restore, and nothing else changes."""
    before = self.queue.snapshot() if self.verify_admission else None
    notified = self.notified
    cancelled = self.queue.cancel(key, name, scope)
    for label in cancelled:
      self.jobs[label].cancelled = True
    if before:
      expected = [entry["task_id"] for entry in before["queued"]
                  if (key is None or tuple(entry["key"]) == tuple(key)) and (name is None or entry["name"] == name)
                  and (scope is None or any(scope == held or held.startswith(scope + ":") for held in entry["scopes"]))
                  and not self.jobs[entry["task_id"]].restore]
      assert cancelled == expected, f"cancelling {key} {name} {scope} cancelled {cancelled}, the model says {expected}"
      after = self.queue.snapshot()
      assert [entry["task_id"] for entry in after["queued"]] == [
          entry["task_id"] for entry in before["queued"] if entry["task_id"] not in expected]
      assert after["running"] == before["running"]
      assert (self.notified > notified) == bool(cancelled), f"the cancel announced {self.notified - notified}"
    return cancelled

  def perform(self, label, scopes, options):
    """A request from `random_operation`: a cancel, or a task to add, after cancelling the pending tasks within
    `clears` (a Delete)."""
    options = dict(options)
    if "cancel" in options:
      return self.cancel(**options["cancel"])
    if "clears" in options:
      self.cancel(scope=options.pop("clears"))
    return self.add(label, *scopes, **options)

  def check_admission(self, job, before, priority):
    """The queue's answer and its queue afterwards against a model written apart from the queue's own code."""
    running, queued, state = before
    running_quiet = {entry["task_id"]: entry["quiet"] for entry in running}
    queue = [(entry["task_id"], tuple(entry["key"]), set(entry["scopes"]), entry["quiet"]) for entry in queued]

    def same(entry_key):
      return entry_key == tuple(job.key)

    outcome, task_id_ = Outcome.ACCEPTED, job.label
    if job.restore:
      if state == QueueState.RUNNING:
        queue.append((job.label, tuple(job.key), set(), False))
      else:
        outcome, task_id_ = Outcome.REFUSED, None
    elif state == QueueState.RESTORING:
      outcome, task_id_ = Outcome.REFUSED, None
    elif state == QueueState.STOPPING:
      outcome = Outcome.REFUSED
    elif job.on_conflict == OnConflict.DEDUP and (
            any(same(key) for _, key, _, _ in queue) or any(same(tuple(entry["key"])) for entry in running)):
      outcome = Outcome.EXISTING
      pending = [index for index, (_, key, _, _) in enumerate(queue) if same(key)]
      if pending:
        task_id_, key, scopes, quiet = queue[pending[0]]
        queue[pending[0]] = (task_id_, key, scopes, quiet and job.quiet)
      else:
        task_id_ = next(entry["task_id"] for entry in running if same(tuple(entry["key"])))
        running_quiet[task_id_] = running_quiet[task_id_] and job.quiet
      if not job.quiet:
        self.jobs[task_id_].quiet_now = False
    elif job.on_conflict == OnConflict.REVISE and any(same(tuple(entry["key"])) for entry in running):
      outcome = Outcome.BUSY
      task_id_ = next(entry["task_id"] for entry in running if same(tuple(entry["key"])))
    else:
      quiet = job.quiet
      if job.on_conflict in (OnConflict.REPLACE, OnConflict.REVISE):
        quiet = quiet and all(entry_quiet for _, key, _, entry_quiet in queue if same(key))
        queue = [entry for entry in queue if not same(entry[1])]
      position = len(queue)
      if priority:
        position = 0
        for _, _, scopes, _ in queue:
          if not covers(scopes, job.scopes):
            break
          position += 1
      queue.insert(position, (job.label, tuple(job.key), set(job.scopes), quiet))
      job.quiet_now = quiet

    assert (job.outcome, job.added.task_id) == (outcome, task_id_), (
        f"{job.label}: {job.added}, the model says {outcome} {task_id_}")
    snapshot = self.queue.snapshot()
    actual_queue = [(entry["task_id"], entry["quiet"]) for entry in snapshot["queued"]]
    assert actual_queue == [(label, quiet) for label, _, _, quiet in queue], (
        f"{job.label}: the queue is {actual_queue}, the model says {queue}")
    actual_running = {entry["task_id"]: entry["quiet"] for entry in snapshot["running"]}
    assert actual_running == running_quiet, f"{job.label}: running {actual_running}, the model says {running_quiet}"

  def expected_starts(self):
    """What a dispatch tick starts, from the queue as it is now, by a model written apart from the queue's own."""
    snapshot = self.queue.snapshot()
    state = self.queue.state
    if state not in (QueueState.RUNNING, QueueState.RESTORING):
      return []
    pending = [entry for entry in snapshot["queued"] if not self.jobs[entry["task_id"]].restore]
    restores = [entry["task_id"] for entry in snapshot["queued"] if self.jobs[entry["task_id"]].restore]
    if restores and not pending and not snapshot["running"]:
      return restores
    held = [set(entry["scopes"]) for entry in snapshot["running"]]
    starts = []
    for entry in pending:
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
      key = (tuple(after), tuple(entry["params"]["label"] for entry in snapshot["queued"]), self.queue.state)
      if self.verify_admission and key != self.seen_key and self.seen_key is not None and self.notified == self.seen_notified:
        self.violations.append(f"the queue went from {self.seen_key} to {key} without a change being announced")
      self.seen_key, self.seen_notified = key, self.notified
      await asyncio.sleep(every)

  def check_snapshot(self, snapshot):
    running = snapshot["running"]
    for index, first in enumerate(running):
      for second in running[index + 1:]:
        if (conflict(first["scopes"], second["scopes"]) or self.jobs[first["task_id"]].restore
                or self.jobs[second["task_id"]].restore):
          self.violations.append(f"{first['task_id']} and {second['task_id']} are both running")
    labels = [entry["task_id"] for entry in running + snapshot["queued"]]
    if len(labels) != len(set(labels)):
      self.violations.append(f"a task is listed twice: {labels}")
    for entry in running + snapshot["queued"]:
      job = self.jobs[entry["task_id"]]
      if (entry["name"], tuple(entry["key"]), entry["labels"]) != (job.name, tuple(job.key), {"Job": job.label}):
        self.violations.append(f"{job.label} is listed as {entry['name']} {entry['key']} {entry['labels']}")

  def verify(self):
    """Every promise at once, once the queue is idle."""
    assert self.violations == []
    assert self.queue.snapshot() == {"running": [], "queued": []}
    assert self.queue.state == QueueState.RUNNING
    assert self.active == {}
    statuses = {}
    for label, status in self.recorded:
      statuses.setdefault(label, []).append(status)
    assert set(statuses) <= set(self.jobs), "a task nobody added was recorded"
    # The tasks a visible request was told would do its work.
    asked_visibly = {job.added.task_id for job in self.jobs.values() if job.outcome == Outcome.EXISTING and not job.quiet}
    for label, job in self.jobs.items():
      recorded = statuses.get(label, [])
      assert job.runs <= 1, f"{label} ran {job.runs} times"
      if job.outcome in (Outcome.EXISTING, Outcome.BUSY):
        assert job.runs == 0 and recorded == [], f"{label} was {job.outcome.value} with {job.added.task_id} but left a trace"
        assert self.jobs[job.added.task_id].key == job.key, f"{label} was given a task with another key"
        continue
      if job.outcome == Outcome.REFUSED:
        assert job.runs == 0, f"{label} was refused but ran"
        expected = ["cancelled"] if job.added.task_id and not job.quiet else []
        assert recorded == expected, f"{label} was refused and recorded {recorded}"
        continue
      if job.runs == 1:
        ending = "failed" if job.fail else "completed"
      else:
        assert self.superseded_later(job) or job.cancelled, f"{label} was accepted and never ran"
        ending = "cancelled"
      if ending == "failed":
        expected = [["failed"]]
      elif job.quiet_now is not None:
        expected = [[] if job.quiet_now else [ending]]
      elif not job.quiet or label in asked_visibly:
        expected = [[ending]]
      elif any(other.key == job.key and not other.quiet for other in self.jobs.values()):
        expected = [[], [ending]]
      else:
        expected = [[]]
      assert recorded in expected, f"{label}: recorded {recorded}, expected one of {expected}"

  def superseded_later(self, job):
    """A pending task is replaced by a later accepted REPLACE or REVISE with the same key, whatever admitted it."""
    return any(other.on_conflict in (OnConflict.REPLACE, OnConflict.REVISE) and other.outcome == Outcome.ACCEPTED
               and other is not job and other.key == job.key for other in self.jobs.values())

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
async def sim():
  sim = Sim()
  yield sim
  await sim.close()


# --- container operations

class TestContainerOperations:
  async def test_containers_deploy_in_parallel(self, sim):
    jobs = [sim.add(f"deploy#{i}", container(i), key=("deploy", f"c{i}"), hold=True) for i in range(10)]
    await sim.settle()
    assert len(sim.running()) == 10 and sim.queued() == []
    for job in jobs:
      job.release()
    await sim.drain()
    sim.verify()
    assert sim.concurrent_max == 10

  async def test_operations_on_one_container_run_one_at_a_time_in_the_order_added(self, sim):
    jobs = [sim.add(f"op#{i}", container(1), key=("op", "c1"), on_conflict=OnConflict.QUEUE, hold=True)
            for i in range(6)]
    for job in jobs:
      await sim.settle()
      assert sim.running() == [job.label]
      job.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == [job.label for job in jobs]

  async def test_a_lifecycle_request_while_one_runs_is_busy_and_accepted_after_it(self, sim):
    first = lifecycle(sim, "deploy#1", 1, "deploy", hold=True)
    await sim.settle()
    for label, name in (("deploy#2", "deploy"), ("stop#1", "stop")):
      busy = lifecycle(sim, label, 1, name)
      assert (busy.outcome, busy.added.task_id) == (Outcome.BUSY, "deploy#1")
    assert sim.queued() == []
    first.release()
    await sim.drain()
    assert lifecycle(sim, "stop#2", 1, "stop").outcome == Outcome.ACCEPTED
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["deploy#1", "stop#2"]

  async def test_the_latest_lifecycle_request_replaces_the_pending_one_while_maintenance_holds_the_container(self, sim):
    backup = sim.add("backup", container(1), key=("backup", "c1"), hold=True)
    await sim.settle()
    for label, name in (("deploy#1", "deploy"), ("stop#1", "stop"), ("deploy#2", "deploy")):
      assert lifecycle(sim, label, 1, name).outcome == Outcome.ACCEPTED
      assert sim.queued() == [label]
    backup.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["backup", "deploy#2"]
    assert sorted(sim.recorded) == [("backup", "completed"), ("deploy#1", "cancelled"), ("deploy#2", "completed"),
                                    ("stop#1", "cancelled")]

  async def test_a_pending_lifecycle_request_is_cancelled_and_a_running_one_is_not(self, sim):
    running = lifecycle(sim, "deploy#1", 1, "deploy", hold=True)
    backup = sim.add("backup", container(2), key=("backup", "c2"), hold=True)
    await sim.settle()
    pending = lifecycle(sim, "stop#2", 2, "stop")
    assert sim.cancel(("lifecycle", "c1")) == []
    assert sim.cancel(("lifecycle", "c2")) == ["stop#2"]
    assert sim.cancel(("lifecycle", "c2")) == []
    assert sim.running() == ["deploy#1", "backup"] and sim.queued() == []
    running.release()
    backup.release()
    await sim.drain()
    sim.verify()
    assert pending.runs == 0 and ("stop#2", "cancelled") in sim.recorded

  async def test_a_delete_clears_the_containers_queued_tasks_and_waits_for_the_running_one(self, sim):
    running = lifecycle(sim, "deploy#1", 1, "deploy", hold=True)
    await sim.settle()
    backup = sim.add("backup", container(1), key=("backup", "c1"))
    other = lifecycle(sim, "deploy#2", 2, "deploy", hold=True)
    delete = sim.perform("delete", [container(1)], dict(key=("delete", "c1"), name="delete",
                                                        on_conflict=OnConflict.REPLACE, clears=container(1)))
    assert (backup.cancelled, other.cancelled, delete.outcome) == (True, False, Outcome.ACCEPTED)
    await sim.settle()
    assert sim.queued() == ["delete"]
    running.release()
    other.release()
    await sim.drain()
    sim.verify()
    assert sim.trace.index(("start", "delete")) > sim.trace.index(("end", "deploy#1"))

  async def test_a_host_wide_change_waits_for_the_hosts_containers_and_holds_them_back_but_not_another_hosts(self, sim):
    a = sim.add("deploy-a", "worker:w1:container:ca", hold=True)
    b = sim.add("deploy-b", "worker:w1:container:cb", hold=True)
    await sim.settle()
    setup = sim.add("setup-w1", "worker:w1", key=("setup", "w1"), hold=True)
    await sim.settle()
    assert sim.queued() == ["setup-w1"]
    a.release()
    await sim.settle()
    assert sim.queued() == ["setup-w1"]
    b.release()
    await sim.settle()
    assert sim.running() == ["setup-w1"]
    late = sim.add("deploy-c", "worker:w1:container:cc")
    other = sim.add("deploy-d", "worker:w2:container:cd")
    await sim.settle()
    assert sim.queued() == ["deploy-c"] and other.runs == 1
    setup.release()
    await sim.drain()
    sim.verify()
    assert sim.starts().index("deploy-c") > sim.starts().index("setup-w1")
    assert late.runs == 1

  async def test_a_failing_deploy_frees_its_container_and_leaves_the_others_alone(self, sim):
    lifecycle(sim, "deploy-a", 1, "deploy", fail="error")
    sim.add("backup-a", container(1), key=("backup", "c1"))
    lifecycle(sim, "deploy-b", 2, "deploy", fail="task")
    lifecycle(sim, "deploy-c", 3, "deploy")
    await sim.drain()
    sim.verify()
    assert sorted(sim.recorded) == [("backup-a", "completed"), ("deploy-a", "failed"), ("deploy-b", "failed"),
                                    ("deploy-c", "completed")]
    assert sim.trace.index(("start", "backup-a")) > sim.trace.index(("end", "deploy-a"))

  async def test_blocking_work_of_many_deploys_is_limited_by_the_size_of_the_lane(self, sim):
    for index in range(10):
      sim.add(f"deploy#{index}", container(index), block=GATE)
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
      sim.add("child-same", container(1), on_conflict=OnConflict.QUEUE)
      sim.add("child-other", container(2))

    parent = sim.add("parent", container(1), hold=True, during=add_children)
    await sim.settle()
    parent.release()
    await sim.drain()
    sim.verify()
    assert sim.trace.index(("end", "parent")) < sim.trace.index(("start", "child-same"))


# --- reconciliations

class TestReconciliations:
  def reconcile(self, sim, label, index, quiet=True, **options):
    return sim.add(label, container(index), key=("reconcile", f"c{index}"), on_conflict=OnConflict.REPLACE,
                   quiet=quiet, **options)

  async def test_requests_during_a_deploy_become_one_that_runs_after_it(self, sim):
    deploy = sim.add("deploy", container(1), hold=True)
    await sim.settle()
    requests = [self.reconcile(sim, f"reconcile#{i}", 1) for i in range(5)]
    assert sim.queued() == ["reconcile#4"]
    assert all(request.outcome == Outcome.ACCEPTED for request in requests)
    deploy.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["deploy", "reconcile#4"]

  async def test_a_request_while_one_runs_keeps_exactly_one_more_run(self, sim):
    first = self.reconcile(sim, "reconcile#1", 1, hold=True)
    await sim.settle()
    self.reconcile(sim, "reconcile#2", 1)
    self.reconcile(sim, "reconcile#3", 1)
    assert sim.running() == ["reconcile#1"] and sim.queued() == ["reconcile#3"]
    first.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["reconcile#1", "reconcile#3"]

  async def test_the_requests_of_different_containers_do_not_replace_each_other(self, sim):
    sim.add("deploy-1", container(1), hold=True)
    sim.add("deploy-2", container(2), hold=True)
    await sim.settle()
    for round_ in range(3):
      self.reconcile(sim, f"reconcile-1#{round_}", 1)
      self.reconcile(sim, f"reconcile-2#{round_}", 2)
    assert sorted(sim.queued()) == ["reconcile-1#2", "reconcile-2#2"]

  async def test_a_deploy_holds_up_only_its_own_container(self, sim):
    sim.add("deploy-1", container(1), hold=True)
    await sim.settle()
    self.reconcile(sim, "reconcile-1", 1)
    self.reconcile(sim, "reconcile-2", 2)
    await sim.until(lambda: "reconcile-2" in sim.starts())
    assert sim.queued() == ["reconcile-1"]

  async def test_a_failing_reconciliation_is_recorded_and_the_next_request_still_runs(self, sim):
    self.reconcile(sim, "reconcile#1", 1, fail="error")
    await sim.drain()
    self.reconcile(sim, "reconcile#2", 1)
    await sim.drain()
    sim.verify()
    assert sim.recorded == [("reconcile#1", "failed")]
    assert sim.starts() == ["reconcile#1", "reconcile#2"]

  async def test_a_flood_of_requests_never_leaves_more_than_one_waiting_per_container(self, sim):
    deploys = [sim.add(f"deploy-{index}", container(index), hold=True) for index in range(6)]
    await sim.settle()
    for request in range(120):
      for index in range(6):
        self.reconcile(sim, f"reconcile-{index}#{request}", index)
      assert len(sim.queued()) == 6
    for deploy in deploys:
      deploy.release()
    await sim.drain()
    sim.verify()
    assert sorted(label for label in sim.starts() if label.startswith("reconcile")) == [
        f"reconcile-{index}#119" for index in range(6)]

  async def test_a_users_request_for_a_quiet_pending_reconciliation_is_recorded_under_its_successor(self, sim):
    deploy = sim.add("deploy", container(1), hold=True)
    await sim.settle()
    self.reconcile(sim, "background", 1)
    self.reconcile(sim, "user", 1, quiet=False)
    self.reconcile(sim, "background#2", 1)
    deploy.release()
    await sim.drain()
    sim.verify()
    assert sorted(sim.recorded) == [("background#2", "completed"), ("deploy", "completed"), ("user", "cancelled")]

  async def test_a_users_request_for_a_running_quiet_task_has_its_completion_recorded(self, sim):
    background = sim.add("background", "worker:observations", key=("observe",), quiet=True, hold=True)
    await sim.settle()
    user = sim.add("user", "worker:observations", key=("observe",))
    assert (user.outcome, user.added.task_id) == (Outcome.EXISTING, "background")
    background.release()
    await sim.drain()
    sim.verify()
    assert sim.recorded == [("background", "completed")]


# --- keys across scopes

class TestKeysAcrossScopes:
  def sync(self, sim, label, worker, **options):
    return sim.add(label, "domain:id:d1", f"worker:{worker}:routing:api", key=("sync_domain", "d1"),
                   on_conflict=OnConflict.REPLACE, quiet=True, **options)

  async def test_a_domain_sync_after_a_move_replaces_the_pending_one_for_the_old_worker(self, sim):
    sim.add("observe", "domain:id:d1", hold=True)
    await sim.settle()
    old = self.sync(sim, "sync-w1", "w1")
    new = self.sync(sim, "sync-w2", "w2")
    assert sim.queued() == ["sync-w2"] and (old.outcome, new.outcome) == (Outcome.ACCEPTED, Outcome.ACCEPTED)
    sim.jobs["observe"].release()
    await sim.drain()
    sim.verify()
    assert old.runs == 0 and new.runs == 1

  async def test_the_successor_on_another_worker_waits_for_the_running_sync_through_the_domain(self, sim):
    running = self.sync(sim, "sync-w1", "w1", hold=True)
    await sim.settle()
    self.sync(sim, "sync-w2", "w2")
    await sim.settle()
    assert sim.running() == ["sync-w1"] and sim.queued() == ["sync-w2"]
    running.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["sync-w1", "sync-w2"]

  async def test_different_keys_on_the_same_scopes_both_queue(self, sim):
    sim.add("hold", container(1), hold=True)
    await sim.settle()
    deploy = lifecycle(sim, "deploy", 1, "deploy")
    backup = sim.add("backup", container(1), key=("backup", "c1"))
    assert (deploy.outcome, backup.outcome) == (Outcome.ACCEPTED, Outcome.ACCEPTED)
    sim.jobs["hold"].release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["hold", "deploy", "backup"]


# --- broad work

class TestBroadWork:
  async def test_certificate_renewal_runs_beside_containers_and_distribution_waits_only_for_it(self, sim):
    renewal = sim.add("renew", "certificate", hold=True)
    deploy = sim.add("deploy", "worker:w1:container:c1", hold=True)
    await sim.settle()
    sim.add("distribute-w1", "certificate", "worker:w1:tls", key=("distribute", "w1"))
    await sim.settle()
    assert sorted(sim.running()) == ["deploy", "renew"] and sim.queued() == ["distribute-w1"]
    renewal.release()
    await sim.settle()
    assert "distribute-w1" in sim.starts()
    deploy.release()
    await sim.drain()
    sim.verify()

  async def test_a_host_wide_change_keeps_waiting_while_the_hosts_containers_overlap_and_starts_at_the_first_gap(self, sim):
    previous = sim.add("overlap#0", "worker:w1:container:c0", hold=True)
    await sim.settle()
    waiting = sim.add("reboot", "worker:w1", key=("reboot", "w1"))
    for round_ in range(1, 15):
      following = sim.add(f"overlap#{round_}", f"worker:w1:container:c{round_ % 3}", hold=True)
      await sim.settle()
      previous.release()
      await sim.settle()
      assert waiting.runs == 0, "the reboot started while a container operation was running"
      previous = following
    previous.release()
    await sim.drain()
    sim.verify()
    assert waiting.runs == 1

  async def test_certificate_work_never_overlaps_and_keeps_its_order(self, sim):
    jobs = [sim.add(f"certificate#{i}", "certificate", key=("certificate", f"{i}"), on_conflict=OnConflict.QUEUE,
                    delay=0.003) for i in range(8)]
    await sim.drain()
    sim.verify()
    assert sim.starts() == [job.label for job in jobs]

  async def test_a_worker_observation_that_is_still_running_is_not_started_twice(self, sim):
    first = sim.add("observe#1", "worker:observations", key=("observe",), quiet=True, hold=True)
    await sim.settle()
    for index in range(2, 6):
      assert sim.add(f"observe#{index}", "worker:observations", key=("observe",), quiet=True).outcome == Outcome.EXISTING
    first.release()
    await sim.drain()
    sim.verify()

  async def test_a_priority_task_goes_behind_the_work_that_covers_it_and_ahead_of_the_rest(self, sim):
    renewal = sim.add("renew", "certificate", hold=True)
    setup = sim.add("setup", "worker:w1", hold=True)
    await sim.settle()
    sim.add("maintenance", "certificate")
    sim.add("reconcile-1", container(1), quiet=True)
    sim.add("reconcile-4", container(4), quiet=True)
    sim.add("urgent", "certificate", priority=True)
    assert sim.queued() == ["maintenance", "urgent", "reconcile-1", "reconcile-4"]
    renewal.release()
    setup.release()
    await sim.drain()
    sim.verify()
    assert sim.starts().index("maintenance") < sim.starts().index("urgent")

  async def test_settings_checks_and_metrics_go_on_beside_a_host_wide_change(self, sim):
    setup = sim.add("setup", "worker:w1", hold=True)
    await sim.settle()
    sim.add("check", "settings:s3", quiet=True, hold=True)
    sim.add("collect", "metrics:manager", quiet=True, hold=True)
    await sim.settle()
    assert sorted(sim.running()) == ["check", "collect", "setup"]
    setup.release()
    sim.jobs["check"].release()
    sim.jobs["collect"].release()
    await sim.drain()
    sim.verify()


# --- restore

class TestRestore:
  async def test_the_tasks_queued_before_a_restore_finish_first_and_every_later_request_is_refused(self, sim):
    deploy = sim.add("deploy-a", container(1), hold=True)
    check = sim.add("check", "settings:s3", hold=True)
    collect = sim.add("collect", "metrics:w1", hold=True)
    await sim.settle()
    sim.add("reconcile-a", container(1), on_conflict=OnConflict.REPLACE, quiet=True)
    backup = sim.add("backup", "backup:store:s1:object:k1", hold=True)
    restore = sim.add("restore", restore=True, hold=True)
    assert sim.queue.state == QueueState.RESTORING
    refused = [sim.add("deploy-b", container(2)), sim.add("restore#2", key=("restore",), restore=True),
               sim.add("reconcile-a#2", container(1), key=("reconcile-a",), on_conflict=OnConflict.REPLACE, quiet=True)]
    assert [job.outcome for job in refused] == [Outcome.REFUSED] * 3
    await sim.settle()
    assert sorted(sim.running()) == ["backup", "check", "collect", "deploy-a"], "a task queued before it starts as usual"
    for job in (deploy, check, backup):
      job.release()
    await sim.settle()
    assert (sim.running(), sim.queued()) == (["collect"], ["restore"]), "the restore waits for every earlier task"
    collect.release()
    await sim.settle()
    assert (sim.running(), sim.queued()) == (["restore"], [])
    assert sim.add("deploy-c", container(3)).outcome == Outcome.REFUSED
    restore.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["deploy-a", "check", "collect", "backup", "reconcile-a", "restore"]
    assert sorted(sim.recorded) == [("backup", "completed"), ("check", "completed"), ("collect", "completed"),
                                    ("deploy-a", "completed"), ("restore", "completed")]

  async def test_what_a_running_task_requests_while_restoring_is_refused(self, sim):
    parent = sim.add("parent", container(1), hold=True,
                     during=lambda job: sim.add("child", container(1), on_conflict=OnConflict.QUEUE))
    await sim.settle()
    sim.add("restore", restore=True)
    parent.release()
    await sim.drain()
    sim.verify()
    assert sim.jobs["child"].outcome == Outcome.REFUSED
    assert sim.starts() == ["parent", "restore"]

  async def test_a_restore_waits_for_a_running_task_that_holds_no_scope(self, sim):
    unscoped = sim.add("unscoped", hold=True)
    await sim.settle()
    restore = sim.add("restore", restore=True)
    await sim.settle()
    assert sim.running() == ["unscoped"] and restore.runs == 0
    unscoped.release()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["unscoped", "restore"]

  @pytest.mark.parametrize("fail", [None, "task", "error"])
  async def test_the_queue_runs_as_usual_after_a_restore_that_succeeded_or_failed(self, sim, fail):
    sim.add("restore", restore=True, fail=fail)
    await sim.drain()
    assert sim.queue.state == QueueState.RUNNING
    assert sim.add("deploy-c", container(3)).outcome == Outcome.ACCEPTED
    await sim.drain()
    sim.verify()
    assert sim.recorded == [("restore", "failed" if fail else "completed"), ("deploy-c", "completed")]


# --- pause and stop

class TestPauseAndStop:
  async def test_a_recurring_task_added_for_a_whole_pause_runs_once_after_it(self, sim):
    sim.queue.pause()
    outcomes = [sim.add(f"observe#{i}", "worker:observations", key=("observe",), quiet=True).outcome
                for i in range(100)]
    assert outcomes.count(Outcome.ACCEPTED) == 1
    await sim.settle()
    assert sim.running() == [] and sim.queued() == ["observe#0"]
    sim.queue.resume()
    await sim.drain()
    sim.verify()
    assert sim.starts() == ["observe#0"]

  async def test_running_tasks_finish_during_a_pause_and_nothing_new_starts(self, sim):
    running = sim.add("deploy-a", container(1), hold=True)
    await sim.settle()
    sim.queue.pause()
    waiting = sim.add("deploy-b", container(2))
    running.release()
    await sim.settle()
    assert sim.running() == [] and waiting.runs == 0
    assert ("deploy-a", "completed") in sim.recorded
    sim.queue.resume()
    await sim.drain()
    sim.verify()

  async def test_stopping_cancels_the_waiting_lets_the_running_finish_and_refuses_what_they_add(self, sim):
    parent = sim.add("parent", container(1), hold=True,
                     during=lambda job: sim.add("child", container(1), on_conflict=OnConflict.QUEUE))
    await sim.settle()
    sim.add("waiting", container(1), on_conflict=OnConflict.QUEUE)
    sim.queue.stop()
    assert sim.recorded == [("waiting", "cancelled")]
    parent.release()
    await sim.until(lambda: not sim.running())
    assert sorted(sim.recorded) == [("child", "cancelled"), ("parent", "completed"), ("waiting", "cancelled")]
    assert sim.jobs["child"].outcome == Outcome.REFUSED
    assert sim.queue.state == QueueState.STOPPING
    assert sim.violations == []


class TestShutdownOfTheLoop:
  async def test_running_tasks_cancelled_by_the_loop_are_recorded_failed_and_free_their_scopes(self, sim):
    jobs = [sim.add(f"deploy#{index}", container(index), hold=True) for index in range(5)]
    sim.add("queued-behind", container(0), on_conflict=OnConflict.QUEUE)
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
      ("common", [f"settings:{key}" for key in "abcdef"], 1),
      ("metrics", [f"metrics:h{index}" for index in range(8)], 2),
      ("app", [container(index) for index in range(12)], 6),
  ])
  async def test_blocking_work_never_exceeds_the_lane_however_many_scopes_are_free(self, sim, sync, lane, scopes, size):
    for index, scope in enumerate(scopes):
      sim.add(f"job#{index}", scope, block=0.03, sync=sync)
    await sim.drain()
    sim.verify()
    assert sim.max_in_thread[lane] == size

  async def test_coroutines_are_not_limited_by_the_size_of_their_lane(self, sim):
    jobs = [sim.add(f"job#{index}", f"settings:k{index}", hold=True) for index in range(40)]
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
    scopes = [container(1), container(2), "settings:x", "metrics:h", "certificate"]
    for index in range(60):
      scope = rng.choice(scopes)
      sim.add(f"job#{index}", scope, on_conflict=OnConflict.QUEUE, delay=rng.uniform(0, 0.01),
              block=rng.choice([None, 0.001]), during=look)
    await sim.drain()
    sim.verify()
    assert len(seen) == 60
    for label, (current, pool) in seen.items():
      assert current == label
      assert pool is sim.pools[sim.jobs[label].lane]


# --- brute force

CONTAINERS = range(8)


def random_operation(rng, index):
  """One request the way the manager makes it: (label, scopes, options)."""
  number = rng.choice(CONTAINERS)
  placement = container(number)
  kind = rng.choices(
      ["deploy", "stop", "cancel", "delete", "reconcile", "user-reconcile", "observe", "setup", "certificate", "backup",
       "check", "metrics", "delete-archive", "domain"],
      weights=[20, 8, 3, 3, 24, 4, 6, 4, 3, 4, 5, 6, 5, 6])[0]
  options = dict(delay=rng.uniform(0, 0.012), fail=rng.choices([None, "task", "error"], [90, 5, 5])[0],
                 block=rng.choice([None, None, rng.uniform(0, 0.006)]), sync=rng.random() < 0.2)
  label = f"{kind}#{index}"
  if kind in ("deploy", "stop"):
    return label, [placement], dict(key=("lifecycle", f"c{number}"), name=kind, on_conflict=OnConflict.REVISE,
                                    priority=kind == "deploy" and rng.random() < 0.1, **options)
  if kind == "cancel":
    operation = rng.choice(["deploy", "stop", "delete"])
    return label, [], dict(cancel=dict(key=("delete" if operation == "delete" else "lifecycle", f"c{number}"), name=operation))
  if kind == "delete":
    return label, [placement], dict(key=("delete", f"c{number}"), name="delete", on_conflict=OnConflict.REPLACE,
                                    clears=placement, **options)
  if kind in ("reconcile", "user-reconcile"):
    return label, [placement], dict(key=("reconcile", f"c{number}"), on_conflict=OnConflict.REPLACE,
                                    quiet=kind == "reconcile", **options)
  if kind == "observe":
    return label, ["worker:observations"], dict(key=("observe",), quiet=rng.random() < 0.8, **options)
  if kind == "setup":
    host = f"w{rng.randrange(3)}"
    return label, [f"worker:{host}"], dict(key=("setup", host), quiet=True, **options)
  if kind == "certificate":
    return label, ["certificate"], dict(key=("renew",), on_conflict=OnConflict.REPLACE, **options)
  if kind == "backup":
    return label, [placement, f"backup:store:s1:object:{index}"], dict(key=("backup", f"c{number}"), **options)
  if kind == "check":
    section = rng.choice("abc")
    return label, [f"settings:{section}"], dict(key=("check", section), on_conflict=OnConflict.REPLACE,
                                                quiet=rng.random() < 0.7, **options)
  if kind == "metrics":
    host = f"h{rng.randrange(3)}"
    return label, [f"metrics:{host}"], dict(key=("collect", host), quiet=True, **options)
  if kind == "delete-archive":
    return label, [f"backup:store:s1:object:{rng.randrange(4)}"], dict(key=(label,), on_conflict=OnConflict.QUEUE,
                                                                       **options)
  domain = f"d{rng.randrange(3)}"
  return label, [f"domain:id:{domain}", f"worker:w{rng.randrange(3)}:routing:{domain}"], dict(
      key=("sync_domain", domain), on_conflict=OnConflict.REPLACE, quiet=True, **options)


SEEDS = range(12)

KEYS = [("deploy", "c1"), ("deploy", "c2"), ("reconcile", "c1"), ("sync_domain", "d1"), ("check", "s3"), ("renew",)]
SCOPE_CHOICES = ["worker", "worker:w1", "worker:w1:container:c1", "worker:w1:container:c2", "worker:w2:container:c3",
                 "domain:id:d1", "certificate", "settings:s3", "metrics:h1", "backup:store:s1"]


def random_scopes(rng):
  if rng.random() < 0.03:
    return []
  return rng.sample(SCOPE_CHOICES, rng.choice([1, 1, 1, 2]))


class TestAdmission:
  """Every request against the model: its outcome and task, and the exact queue afterwards."""

  @pytest.mark.parametrize("seed", range(8))
  async def test_a_storm_of_requests_while_nothing_runs(self, sim, seed):
    rng = random.Random(seed)
    sim.queue.pause()
    for index in range(600):
      if rng.random() < 0.05:
        sim.cancel(**rng.choice([dict(key=rng.choice([*KEYS, ("restore",)])), dict(scope=rng.choice(SCOPE_CHOICES)),
                                 dict(key=rng.choice(KEYS), name=rng.choice(["deploy", "reconcile", "check"]))]))
        continue
      restore = rng.random() < 0.03
      scopes = [] if restore else random_scopes(rng)
      sim.add(f"op#{index}", *scopes, key=("restore",) if restore else rng.choice(KEYS),
              on_conflict=rng.choice(list(OnConflict)), quiet=rng.random() < 0.4, priority=rng.random() < 0.2,
              restore=restore)
    sim.queue.resume()
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()

  @pytest.mark.parametrize("seed", range(8))
  async def test_a_storm_of_requests_while_tasks_start_and_end(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    for index in range(500):
      if rng.random() < 0.05:
        sim.cancel(**rng.choice([dict(key=rng.choice(KEYS)), dict(scope=rng.choice(SCOPE_CHOICES))]))
        continue
      sim.add(f"op#{index}", *random_scopes(rng), key=rng.choice(KEYS), on_conflict=rng.choice(list(OnConflict)),
              quiet=rng.random() < 0.4, priority=rng.random() < 0.2, delay=rng.uniform(0, 0.006),
              fail=rng.choice([None, None, None, "error"]))
      await asyncio.sleep(rng.uniform(0, 0.002))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()

  async def test_the_requests_after_a_stop_are_all_refused(self, sim):
    sim.queue.stop()
    rng = random.Random(1)
    for index in range(50):
      job = sim.add(f"late#{index}", rng.choice(SCOPE_CHOICES), key=rng.choice(KEYS),
                    on_conflict=rng.choice(list(OnConflict)), quiet=rng.random() < 0.4)
      assert job.outcome == Outcome.REFUSED
    assert sim.queued() == []
    assert sorted(label for label, status in sim.recorded if status == "cancelled") == sorted(
        f"late#{index}" for index in range(50) if not sim.jobs[f"late#{index}"].quiet)


class TestBruteForce:
  @pytest.mark.parametrize("seed", SEEDS)
  async def test_container_domain_and_broad_work_crossing_at_random(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    for index in range(140):
      label, scopes, options = random_operation(rng, index)
      sim.perform(label, scopes, options)
      await asyncio.sleep(rng.uniform(0, 0.004))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert sim.concurrent_max >= 3, "the workload never ran in parallel"
    for lane, size in POOLS.items():
      assert sim.max_in_thread[lane] <= size

  @pytest.mark.parametrize("policy", [OnConflict.DEDUP, OnConflict.REPLACE, OnConflict.REVISE])
  async def test_threads_racing_to_add_the_same_task_leave_exactly_one(self, sim, eager_threads, policy):
    sim.verify_admission = False
    sim.queue.pause()
    threads, rounds = 8, 150
    barrier = threading.Barrier(threads)

    def race(number):
      for round_ in range(rounds):
        barrier.wait()
        sim.add(f"round{round_}-t{number}", container(round_ % 5), key=("race", f"{round_}"), on_conflict=policy)

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=threads) as producers:
      await asyncio.gather(*(loop.run_in_executor(producers, race, number) for number in range(threads)))
    pending = sim.queued()
    assert len(pending) == rounds, f"{len(pending)} pending for {rounds} different tasks"
    assert len({label.split("-")[0] for label in pending}) == rounds
    accepted = [job for job in sim.jobs.values() if job.outcome == Outcome.ACCEPTED]
    assert len(accepted) == (rounds if policy == OnConflict.DEDUP else rounds * threads)
    if policy == OnConflict.DEDUP:
      for job in sim.jobs.values():
        assert job.added.task_id in pending and sim.jobs[job.added.task_id].key == job.key
    sim.queue.resume()
    await sim.drain()
    sim.verify()

  @pytest.mark.parametrize("seed", range(4))
  async def test_threads_making_quiet_work_visible_while_it_finishes_never_lose_its_record(self, sim, eager_threads, seed):
    sim.verify_admission = False
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    threads, per_thread = 6, 60

    def produce(number):
      rng = random.Random(seed * 100 + number)
      for index in range(per_thread):
        sim.add(f"t{number}-observe#{index}", "worker:observations", key=("observe",), quiet=rng.random() < 0.7,
                block=rng.choice([None, rng.uniform(0, 0.002)]), sync=rng.random() < 0.3)
        time.sleep(rng.uniform(0, 0.001))

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=threads) as producers:
      await asyncio.gather(*(loop.run_in_executor(producers, produce, number) for number in range(threads)))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert any(job.outcome == Outcome.EXISTING and not job.quiet and sim.jobs[job.added.task_id].quiet
               for job in sim.jobs.values()), "no visible request ever joined a quiet task"

  @pytest.mark.parametrize("seed", range(4))
  async def test_threads_requesting_and_cancelling_lifecycles_leave_one_pending_request_per_container(
          self, sim, eager_threads, seed):
    sim.verify_admission = False
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    threads, per_thread = 6, 60
    crowded = []

    def produce(number):
      rng = random.Random(seed * 100 + number)
      for index in range(per_thread):
        placement = rng.randrange(3)
        if rng.random() < 0.2:
          sim.cancel(key=("lifecycle", f"c{placement}"))
        else:
          name = rng.choice(["deploy", "stop"])
          lifecycle(sim, f"t{number}-{name}#{index}", placement, name, delay=rng.uniform(0, 0.004),
                    sync=rng.random() < 0.2)
        pending = [entry["key"] for entry in sim.queue.snapshot()["queued"]]
        if any(pending.count(key) > 1 for key in pending):
          crowded.append(pending)
        time.sleep(rng.uniform(0, 0.001))

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=threads) as producers:
      await asyncio.gather(*(loop.run_in_executor(producers, produce, number) for number in range(threads)))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert crowded == []
    assert any(job.outcome == Outcome.BUSY for job in sim.jobs.values()), "no request found its container busy"
    assert any(job.cancelled for job in sim.jobs.values()), "no cancel found a pending request"

  @pytest.mark.parametrize("seed", range(4))
  async def test_threads_adding_while_the_loop_dispatches(self, sim, eager_threads, seed):
    sim.verify_admission = False
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    threads, per_thread = 6, 50
    added = []

    def produce(number):
      rng = random.Random(seed * 100 + number)
      for index in range(per_thread):
        label, scopes, options = random_operation(rng, number * per_thread + index)
        sim.perform(f"t{number}-{label}", scopes, options)
        if "cancel" not in options:
          added.append(label)
        time.sleep(rng.uniform(0, 0.002))

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=threads) as producers:
      await asyncio.gather(*(loop.run_in_executor(producers, produce, number) for number in range(threads)))
    await sim.drain()
    stop.set()
    await ticker
    assert len(sim.jobs) == len(added)
    sim.verify()

  @pytest.mark.parametrize("seed", range(6))
  async def test_operations_on_one_container_keep_the_order_they_were_added_in(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    added = {number: [] for number in range(4)}
    for index in range(120):
      number = rng.choice(list(added))
      label = f"op#{index}"
      sim.add(label, container(number), on_conflict=OnConflict.QUEUE, delay=rng.uniform(0, 0.008),
              fail=rng.choice([None, None, None, "error"]))
      added[number].append(label)
      await asyncio.sleep(rng.uniform(0, 0.002))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    starts = sim.starts()
    for labels in added.values():
      assert [label for label in starts if label in set(labels)] == labels

  @pytest.mark.parametrize("seed", range(6))
  async def test_a_restore_in_the_middle_of_the_storm(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    others_started_during = []

    def end(job):
      during = [label for kind, label in sim.trace[sim.trace.index(("start", "restore")) + 1:] if kind == "start"]
      others_started_during.extend(during)
      sim.queue.end_restore()

    restore_at = rng.randrange(40, 100)
    for index in range(140):
      label, scopes, options = random_operation(rng, index)
      sim.perform(label, scopes, options)
      if index == restore_at:
        sim.add("restore", restore=True, delay=0.02, during=end, fail=rng.choice([None, "task"]))
        sim.cancel(key=("restore",))
      await asyncio.sleep(rng.uniform(0, 0.003))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert sim.jobs["restore"].runs == 1
    assert others_started_during == [], "something started while the restore ran"
    labels = list(sim.jobs)
    before = set(labels[:labels.index("restore")])
    started_after = {label for kind, label in sim.trace[sim.trace.index(("start", "restore")):] if kind == "start"}
    assert not before & started_after, "a task queued before the restore started after it"
    assert any(sim.jobs[label].outcome == Outcome.REFUSED for label in labels[labels.index("restore") + 1:]), (
        "no request came while restoring")

  @pytest.mark.parametrize("seed", range(6))
  async def test_pausing_and_resuming_at_random(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    for index in range(140):
      label, scopes, options = random_operation(rng, index)
      sim.perform(label, scopes, options)
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
  async def test_reconciliations_cannot_pile_up_behind_deploys_that_cross(self, sim, seed):
    rng = random.Random(seed)
    stop = asyncio.Event()
    ticker = asyncio.create_task(sim.run_ticker(stop))
    waiting_most = 0
    for index in range(200):
      number = rng.choice(CONTAINERS)
      if rng.random() < 0.15:
        lifecycle(sim, f"deploy#{index}", number, "deploy", delay=rng.uniform(0.01, 0.04))
      sim.add(f"reconcile#{index}", container(number), key=("reconcile", f"c{number}"),
              on_conflict=OnConflict.REPLACE, quiet=True, delay=rng.uniform(0, 0.006))
      pending = [entry for entry in sim.queue.snapshot()["queued"] if entry["name"] == "reconcile"]
      assert len(pending) <= len(CONTAINERS), "more than one reconciliation waits for a container"
      waiting_most = max(waiting_most, len(pending))
      await asyncio.sleep(rng.uniform(0, 0.003))
    await sim.drain()
    stop.set()
    await ticker
    sim.verify()
    assert waiting_most >= 1, "the reconciliations never had to wait"
