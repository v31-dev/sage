import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from utils.executor import active_executor
from utils.logging import TaskFailed, task_id
from utils.queue import Added, OnConflict, Outcome, QueuedTask, QueueState, TaskQueue, admit, next_to_start


def queued(name, *scopes, key=None, quiet=False):
  return QueuedTask(name=name, key=key or (name,), scopes=frozenset(scopes), task=lambda: None, executor="app",
                    task_id=f"{name}-{'-'.join(sorted(scopes))}", quiet=quiet)


RESTORE = queued("restore")


def admission(new, *, state=QueueState.RUNNING, running=(), queue=(), on_conflict=OnConflict.DEDUP):
  return admit(state, list(running), list(queue), new, on_conflict)


class TestAdmit:
  def test_a_task_on_an_empty_queue_is_accepted(self):
    new = queued("deploy", "worker:w1:container:c1")
    result = admission(new)
    assert (result.outcome, result.task, result.superseded) == (Outcome.ACCEPTED, new, ())

  @pytest.mark.parametrize("where", ["running", "queue"])
  def test_dedup_returns_the_pending_or_running_task_with_the_same_key(self, where):
    same = queued("deploy", "worker:w1:container:c1", key=("deploy", "c1"))
    result = admission(queued("deploy", "worker:w1:container:c1", key=("deploy", "c1")), **{where: [same]})
    assert (result.outcome, result.task) == (Outcome.EXISTING, same)

  def test_dedup_returns_the_pending_task_rather_than_the_running_one(self):
    running, pending = queued("sync", "domain:id:d1", key=("sync", "d1")), queued("next", "domain:id:d1", key=("sync", "d1"))
    result = admission(queued("sync", "domain:id:d1", key=("sync", "d1")), running=[running], queue=[pending])
    assert result.task is pending

  def test_dedup_matches_the_key_whatever_the_scopes(self):
    old = queued("sync", "domain:id:d1", "worker:w1:routing:api", key=("sync", "d1"))
    result = admission(queued("sync", "domain:id:d1", "worker:w2:routing:api", key=("sync", "d1")), queue=[old])
    assert (result.outcome, result.task) == (Outcome.EXISTING, old)

  def test_dedup_accepts_another_key_on_the_same_scopes(self):
    other = queued("deploy", "worker:w1:container:c1", key=("deploy", "c1"))
    result = admission(queued("stop", "worker:w1:container:c1", key=("stop", "c1")), queue=[other])
    assert result.outcome == Outcome.ACCEPTED

  @pytest.mark.parametrize("where", ["running", "queue"])
  def test_queue_accepts_a_task_with_the_same_key(self, where):
    result = admission(queued("delete", "backup:store:s1"), on_conflict=OnConflict.QUEUE,
                       **{where: [queued("delete", "backup:store:s1")]})
    assert (result.outcome, result.superseded) == (Outcome.ACCEPTED, ())

  def test_replace_supersedes_the_pending_task_with_the_key_even_on_other_scopes(self):
    old = queued("sync", "domain:id:d1", "worker:w1:routing:api", key=("sync", "d1"))
    new = queued("sync", "domain:id:d1", "worker:w2:routing:api", key=("sync", "d1"))
    result = admission(new, on_conflict=OnConflict.REPLACE, queue=[old, queued("x", "worker:w3")])
    assert (result.outcome, result.task, result.superseded) == (Outcome.ACCEPTED, new, (old,))

  def test_replace_leaves_a_running_task_with_the_key_alone(self):
    result = admission(queued("sync", "domain:id:d1"), on_conflict=OnConflict.REPLACE,
                       running=[queued("sync", "domain:id:d1")])
    assert (result.outcome, result.superseded) == (Outcome.ACCEPTED, ())

  def test_replace_leaves_another_key_alone(self):
    result = admission(queued("sync", "domain:id:d1", key=("sync", "d1")), on_conflict=OnConflict.REPLACE,
                       queue=[queued("sync", "domain:id:d1", key=("sync", "d2"))])
    assert result.superseded == ()

  def test_revise_is_busy_with_the_running_task_with_the_key_whatever_its_name(self):
    current = queued("deploy", "worker:w1:container:c1", key=("lifecycle", "c1"))
    result = admission(queued("stop", "worker:w1:container:c1", key=("lifecycle", "c1")), on_conflict=OnConflict.REVISE,
                       running=[current])
    assert (result.outcome, result.task, result.superseded) == (Outcome.BUSY, current, ())

  def test_revise_supersedes_the_pending_task_with_the_key_whatever_its_name(self):
    old = queued("deploy", "worker:w1:container:c1", key=("lifecycle", "c1"))
    new = queued("stop", "worker:w1:container:c1", key=("lifecycle", "c1"))
    result = admission(new, on_conflict=OnConflict.REVISE, queue=[old, queued("x", "worker:w3")])
    assert (result.outcome, result.task, result.superseded) == (Outcome.ACCEPTED, new, (old,))

  def test_revise_is_accepted_beside_a_running_task_with_another_key_on_its_scope(self):
    result = admission(queued("deploy", "worker:w1:container:c1", key=("lifecycle", "c1")),
                       on_conflict=OnConflict.REVISE, running=[queued("backup", "worker:w1:container:c1")])
    assert (result.outcome, result.superseded) == (Outcome.ACCEPTED, ())

  def test_a_visible_request_makes_the_quiet_task_with_its_key_visible(self):
    quiet = queued("reconcile", "worker:w1:container:c1", quiet=True)
    assert admission(queued("reconcile", "worker:w1:container:c1"), queue=[quiet]).reveal is True

  @pytest.mark.parametrize("existing_quiet, new_quiet", [(True, True), (False, True), (False, False)])
  def test_nothing_is_revealed_otherwise(self, existing_quiet, new_quiet):
    existing = queued("reconcile", "worker:w1:container:c1", quiet=existing_quiet)
    assert admission(queued("reconcile", "worker:w1:container:c1", quiet=new_quiet), running=[existing]).reveal is False

  @pytest.mark.parametrize("on_conflict", [OnConflict.REPLACE, OnConflict.REVISE])
  @pytest.mark.parametrize("superseded_quiet, new_quiet, expected", [
      ((True, True), True, True), ((True, False), True, False), ((True,), False, False), ((), True, True)])
  def test_a_replacement_is_visible_when_it_or_a_task_it_supersedes_is(self, superseded_quiet, new_quiet, expected,
                                                                       on_conflict):
    queue = [queued("reconcile", "worker:w1:container:c1", quiet=quiet) for quiet in superseded_quiet]
    result = admission(queued("reconcile", "worker:w1:container:c1", quiet=new_quiet), queue=queue,
                       on_conflict=on_conflict)
    assert result.task.quiet is expected

  def test_a_task_is_accepted_while_paused(self):
    assert admission(queued("a", "worker:w1"), state=QueueState.PAUSED).outcome == Outcome.ACCEPTED

  def test_dedup_still_keeps_one_pending_copy_while_paused(self):
    result = admission(queued("sync", "worker:observations"), state=QueueState.PAUSED,
                       queue=[queued("sync", "worker:observations")])
    assert result.outcome == Outcome.EXISTING

  def test_a_stopping_queue_refuses_what_is_added_and_records_it_cancelled(self):
    result = admission(queued("a", "worker:w1"), state=QueueState.STOPPING)
    assert (result.outcome, result.cancelled) == (Outcome.REFUSED, True)

  @pytest.mark.parametrize("on_conflict", list(OnConflict))
  def test_a_restoring_queue_refuses_every_request_without_a_record(self, on_conflict):
    pending = queued("sync", "domain:id:d1", key=("sync", "d1"), quiet=True)
    result = admission(queued("sync", "domain:id:d1", key=("sync", "d1")), state=QueueState.RESTORING, queue=[pending],
                       on_conflict=on_conflict)
    assert (result.outcome, result.task, result.superseded, result.reveal, result.cancelled) == (
        Outcome.REFUSED, None, (), False, False)


class TestNextToStart:
  def names(self, state, queue, running=(), restore=None):
    return [task.name for task in next_to_start(state, list(queue), list(running), restore)]

  def test_tasks_on_sibling_scopes_start_together(self):
    queue = [queued("a", "worker:w1:container:c1"), queued("b", "worker:w1:container:c2"), queued("c", "settings:s3")]
    assert self.names(QueueState.RUNNING, queue) == ["a", "b", "c"]

  def test_a_task_on_a_scope_that_is_running_waits(self):
    queue = [queued("b", "worker:w1:container:c1"), queued("c", "worker:w1:container:c2")]
    assert self.names(QueueState.RUNNING, queue, running=[queued("a", "worker:w1:container:c1")]) == ["c"]

  def test_the_second_of_two_tasks_on_one_scope_waits_for_the_first_started_in_the_same_scan(self):
    queue = [queued("a", "certificate"), queued("b", "certificate")]
    assert self.names(QueueState.RUNNING, queue) == ["a"]

  def test_a_parent_scope_waits_for_a_running_child_without_holding_back_the_children_after_it(self):
    queue = [queued("host", "worker:w1"), queued("b", "worker:w1:container:c2")]
    assert self.names(QueueState.RUNNING, queue, running=[queued("a", "worker:w1:container:c1")]) == ["b"]

  def test_a_parent_scope_that_starts_holds_back_the_children_after_it(self):
    queue = [queued("host", "worker:w1"), queued("b", "worker:w1:container:c2")]
    assert self.names(QueueState.RUNNING, queue) == ["host"]

  def test_a_child_started_earlier_in_the_scan_holds_the_parent_scope_back(self):
    queue = [queued("a", "worker:w1:container:c1"), queued("host", "worker:w1"), queued("c", "certificate")]
    assert self.names(QueueState.RUNNING, queue) == ["a", "c"]

  def test_scopes_that_only_share_a_prefix_do_not_conflict(self):
    queue = [queued("a", "worker:w1"), queued("b", "worker:w10"), queued("c", "worker:w")]
    assert self.names(QueueState.RUNNING, queue) == ["a", "b", "c"]

  def test_a_task_holding_several_scopes_conflicts_on_any_of_them(self):
    queue = [queued("both", "certificate", "worker:w1:tls")]
    assert self.names(QueueState.RUNNING, queue, running=[queued("host", "worker:w1")]) == []
    assert self.names(QueueState.RUNNING, queue, running=[queued("c", "worker:w1:container:c1")]) == ["both"]

  def test_tasks_without_scopes_start_together(self):
    assert self.names(QueueState.RUNNING, [queued("a"), queued("b")]) == ["a", "b"]

  def test_while_restoring_the_tasks_queued_before_the_restore_start_as_usual(self):
    queue = [queued("a", "worker:w1"), queued("b", "worker:w1:container:c1"), queued("c", "settings:s3")]
    assert self.names(QueueState.RESTORING, queue, running=[queued("p", "certificate")], restore=RESTORE) == ["a", "c"]

  @pytest.mark.parametrize("queue, running, expected", [
      ([queued("a", "settings:s3")], [], ["a"]), ([queued("b", "worker:w1")], [queued("c", "worker:w1:container:c1")], []),
      ([], [queued("p", "certificate")], []), ([], [queued("none")], [])])
  def test_the_restore_waits_while_any_task_runs_or_waits(self, queue, running, expected):
    assert self.names(QueueState.RESTORING, queue, running, restore=RESTORE) == expected

  def test_the_restore_starts_alone_once_nothing_runs_or_waits(self):
    assert self.names(QueueState.RESTORING, [], restore=RESTORE) == ["restore"]

  @pytest.mark.parametrize("state", [QueueState.PAUSED, QueueState.STOPPING])
  def test_nothing_starts_while_paused_or_stopping(self, state):
    assert self.names(state, [queued("a", "worker:w1")]) == []

  def test_an_empty_queue_starts_nothing(self):
    assert self.names(QueueState.RUNNING, []) == []


ROOTS = frozenset(["worker", "certificate", "settings"])


class Lane:
  """A queue over two pools with its records kept."""

  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lane")
    self.recorded = []
    self.recorded_tasks = []
    self.changes = 0
    self.queue = TaskQueue(ROOTS, {"platform": self.pool, "app": self.pool}, record=self.record, notify=self.changed)

  def record(self, task, status):
    self.recorded.append((task.name, status))
    self.recorded_tasks.append(task)

  def changed(self):
    self.changes += 1

  def add(self, task, key=None, scope="worker:w1", **kwargs):
    kwargs.setdefault("executor", "app")
    kwargs.setdefault("scopes", {scope})
    return self.queue.add_task(task=task, key=key or (task.__name__,), **kwargs)

  async def run(self, ticks=1000):
    """Start what can start and wait until nothing is running, failing after `ticks` ticks."""
    self.queue.dispatch_tick()
    for _ in range(ticks):
      if not self.queue.snapshot()["running"]:
        return
      await asyncio.sleep(0)
      self.queue.dispatch_tick()
    raise AssertionError(f"still running after {ticks} ticks: {self.queue.snapshot()['running']}")


@pytest.fixture
def lane():
  lane = Lane()
  yield lane
  lane.pool.shutdown(wait=False, cancel_futures=True)


async def done():
  pass


async def fails():
  raise TaskFailed()


async def crashes():
  raise RuntimeError("boom")


def holding():
  """A task that runs until the event it returns is set."""
  release = asyncio.Event()

  async def holds():
    await release.wait()

  return holds, release


class TestRunTask:
  async def test_a_task_that_completes_is_recorded_and_no_longer_running(self, lane):
    lane.add(done, key=("deploy", "c1"))
    await lane.run()
    assert lane.recorded == [("deploy", "completed")]
    assert lane.queue.snapshot() == {"running": [], "queued": []}

  async def test_a_quiet_task_is_recorded_only_when_it_fails(self, lane):
    lane.add(done, key=("ok",), quiet=True)
    lane.add(fails, key=("bad",), quiet=True, scope="worker:w2")
    await lane.run()
    assert lane.recorded == [("bad", "failed")]

  async def test_a_task_that_raises_task_failed_is_failed_without_a_traceback(self, lane, caplog):
    lane.add(fails, key=("bad",))
    with caplog.at_level(logging.INFO):
      await lane.run()
    assert lane.recorded == [("bad", "failed")]
    record = next(r for r in caplog.records if r.getMessage() == "bad: failed")
    assert record.levelno == logging.ERROR and record.exc_info is None

  async def test_a_task_that_raises_anything_else_is_failed_with_its_traceback(self, lane, caplog):
    lane.add(crashes, key=("bad",))
    with caplog.at_level(logging.INFO):
      await lane.run()
    assert lane.recorded == [("bad", "failed")]
    record = next(r for r in caplog.records if r.getMessage() == "bad: failed")
    assert record.exc_info is not None

  async def test_a_failed_task_frees_its_scope_for_the_next(self, lane):
    lane.add(crashes, key=("bad",))
    lane.add(done, key=("next",))
    await lane.run()
    assert lane.recorded == [("bad", "failed"), ("next", "completed")]

  async def test_a_task_cancelled_by_the_loop_shutting_down_is_failed(self, lane):
    started = asyncio.Event()

    async def hangs():
      started.set()
      await asyncio.sleep(3600)

    lane.add(hangs)
    lane.queue.dispatch_tick()
    await started.wait()
    handles = list(lane.queue._tasks)
    for handle in handles:
      handle.cancel()
    await asyncio.gather(*handles, return_exceptions=True)
    assert lane.recorded == [("hangs", "failed")]
    assert lane.queue.snapshot()["running"] == []

  async def test_a_task_runs_with_its_id_and_its_lane_bound(self, lane):
    seen = {}

    async def look():
      seen["id"] = task_id.get()
      seen["pool"] = active_executor.get()

    lane.add(look, task_id="t-1")
    await lane.run()
    assert seen == {"id": "t-1", "pool": lane.pool}

  async def test_a_sync_task_runs_on_its_lane_with_its_params_and_inputs(self, lane):
    seen = {}

    def work(value, snapshot):
      seen["thread"] = threading.current_thread().name
      seen["arguments"] = (value, snapshot)

    snapshot = object()
    lane.add(work, params={"value": 7}, inputs={"snapshot": snapshot})
    await lane.run()
    assert seen["thread"].startswith("lane")
    assert seen["arguments"] == (7, snapshot)

  async def test_a_task_gets_its_inputs_but_the_record_and_the_snapshot_do_not(self, lane):
    seen = {}
    work, release = holding()

    async def deploy(container_id, snapshot):
      seen["snapshot"] = snapshot
      await work()

    lane.add(deploy, params={"container_id": "c1"}, inputs={"snapshot": {"secret": "s"}})
    lane.queue.dispatch_tick()
    await asyncio.sleep(0)
    [running] = lane.queue.snapshot()["running"]
    assert seen["snapshot"] == {"secret": "s"}
    assert "inputs" not in running and running["params"] == {"container_id": "c1"}
    release.set()
    await lane.run()
    [task] = lane.recorded_tasks
    assert task.params == {"container_id": "c1"}


class TestVisibility:
  """A visible request for the work a quiet task does makes that task visible."""

  async def test_a_pending_quiet_task_is_recorded_once_a_visible_request_joins_it(self, lane):
    first = lane.add(done, key=("reconcile", "c1"), quiet=True)
    second = lane.add(done, key=("reconcile", "c1"))
    assert second == Added(Outcome.EXISTING, first.task_id)
    assert [task["quiet"] for task in lane.queue.snapshot()["queued"]] == [False]
    await lane.run()
    assert lane.recorded == [("reconcile", "completed")]
    assert lane.recorded_tasks[0].task_id == first.task_id

  async def test_a_running_quiet_task_is_recorded_once_a_visible_request_joins_it(self, lane):
    work, release = holding()
    first = lane.add(work, key=("reconcile", "c1"), quiet=True)
    lane.queue.dispatch_tick()
    assert lane.add(done, key=("reconcile", "c1")) == Added(Outcome.EXISTING, first.task_id)
    assert [task["quiet"] for task in lane.queue.snapshot()["running"]] == [False]
    release.set()
    await lane.run()
    assert lane.recorded == [("reconcile", "completed")]

  async def test_a_quiet_request_leaves_a_quiet_task_quiet(self, lane):
    lane.add(done, key=("reconcile", "c1"), quiet=True)
    lane.add(done, key=("reconcile", "c1"), quiet=True)
    await lane.run()
    assert lane.recorded == []

  async def test_a_quiet_replacement_of_a_visible_task_is_visible(self, lane):
    lane.add(done, key=("sync", "d1"), on_conflict=OnConflict.REPLACE)
    lane.add(done, key=("sync", "d1"), on_conflict=OnConflict.REPLACE, quiet=True)
    await lane.run()
    assert lane.recorded == [("sync", "cancelled"), ("sync", "completed")]


class TestTimes:
  async def test_a_task_keeps_when_it_was_queued_and_when_it_started(self, lane, clock):
    lane.add(done, key=("deploy", "c1"))
    [waiting] = lane.queue.snapshot()["queued"]
    assert (waiting["queued_at"], waiting["started_at"]) == (datetime(2026, 1, 1, 12, 0), None)

    clock.advance(timedelta(minutes=5))
    lane.queue.dispatch_tick()
    [running] = lane.queue.snapshot()["running"]
    assert (running["queued_at"], running["started_at"]) == (datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 12, 5))

    await lane.run()
    [task] = lane.recorded_tasks
    assert (task.queued_at, task.started_at) == (datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 12, 5))

  def test_a_task_cancelled_before_it_started_has_no_start(self, lane, clock):
    lane.add(done, key=("sync",), on_conflict=OnConflict.REPLACE)
    clock.advance(timedelta(minutes=1))
    lane.add(done, key=("sync",), on_conflict=OnConflict.REPLACE)

    [task] = lane.recorded_tasks
    assert (task.queued_at, task.started_at) == (datetime(2026, 1, 1, 12, 0), None)


class TestAddTask:
  def test_it_is_refused_for_an_unknown_scope_root_or_lane(self, lane):
    with pytest.raises(ValueError, match="Unknown scope root 'nowhere'"):
      lane.add(done, scope="nowhere:a")
    with pytest.raises(ValueError, match="Unknown scope root '\\*'"):
      lane.add(done, scope="*:a")
    with pytest.raises(ValueError, match="Unknown scope root '\\*'"):
      lane.add(done, scope="*")
    with pytest.raises(ValueError, match="Unknown executor"):
      lane.add(done, executor="nowhere")

  @pytest.mark.parametrize("key", [(), ("",), ("deploy", 3), ("deploy", None)])
  def test_a_key_is_a_tuple_of_names(self, lane, key):
    with pytest.raises(ValueError, match="A task key is a tuple of names"):
      lane.queue.add_task(task=done, key=key, scopes={"worker:w1"}, executor="app")

  def test_params_are_json(self, lane):
    with pytest.raises(ValueError, match="must be JSON"):
      lane.add(done, params={"session": object()})

  def test_an_argument_is_a_param_or_an_input(self, lane):
    with pytest.raises(ValueError, match="both a param and an input: \\['container_id'\\]"):
      lane.add(done, params={"container_id": "c1"}, inputs={"container_id": "c1"})

  @pytest.mark.parametrize("labels", [{"Application": 3}, {1: "w1"}])
  def test_labels_are_text(self, lane, labels):
    with pytest.raises(ValueError, match="labels name each subject in text"):
      lane.add(done, labels=labels)

  def test_it_says_what_it_did_with_each_request(self, lane):
    accepted = lane.add(done, key=("a",), task_id="t1")
    assert accepted == Added(Outcome.ACCEPTED, "t1")
    assert lane.add(done, key=("a",), task_id="t2") == Added(Outcome.EXISTING, "t1")
    assert lane.add(done, key=("a",), task_id="t3", on_conflict=OnConflict.QUEUE) == Added(Outcome.ACCEPTED, "t3")
    assert lane.queue.request_restore(done, "restore", "platform", task_id="t4") == Added(Outcome.ACCEPTED, "t4")
    assert lane.queue.request_restore(done, "restore", "platform", task_id="t5") == Added(Outcome.REFUSED)
    assert lane.add(done, key=("b",), task_id="t6") == Added(Outcome.REFUSED)
    lane.queue.stop()
    assert lane.add(done, key=("c",), task_id="t7") == Added(Outcome.REFUSED, "t7")

  async def test_a_request_while_a_task_with_its_key_runs_is_busy_with_its_id_and_leaves_no_trace(self, lane):
    work, release = holding()
    lane.add(work, key=("lifecycle", "c1"), name="deploy", task_id="t1", on_conflict=OnConflict.REVISE)
    lane.queue.dispatch_tick()
    changes = lane.changes
    assert lane.add(done, key=("lifecycle", "c1"), name="stop", task_id="t2",
                    on_conflict=OnConflict.REVISE) == Added(Outcome.BUSY, "t1")
    assert (lane.queue.snapshot()["queued"], lane.recorded, lane.changes) == ([], [], changes)
    release.set()
    await lane.run()
    assert lane.recorded == [("deploy", "completed")]

  def test_a_task_is_named_by_the_start_of_its_key_unless_named(self, lane):
    lane.add(done, key=("lifecycle", "c1"), name="deploy")
    lane.add(done, key=("sync_domain", "d1"))
    assert [(task["name"], task["key"]) for task in lane.queue.snapshot()["queued"]] == [
        ("deploy", ["lifecycle", "c1"]), ("sync_domain", ["sync_domain", "d1"])]
    lane.add(done, key=("lifecycle", "c1"), name="stop", on_conflict=OnConflict.REVISE)
    assert lane.recorded == [("deploy", "cancelled")]

  def test_a_replaced_pending_task_is_recorded_as_cancelled(self, lane):
    lane.add(done, key=("sync",), on_conflict=OnConflict.REPLACE)
    lane.add(done, key=("sync",), on_conflict=OnConflict.REPLACE)
    assert lane.recorded == [("sync", "cancelled")]
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["sync"]

  def test_a_quiet_task_that_is_replaced_leaves_no_record(self, lane):
    lane.add(done, key=("sync",), on_conflict=OnConflict.REPLACE, quiet=True)
    lane.add(done, key=("sync",), on_conflict=OnConflict.REPLACE, quiet=True)
    assert lane.recorded == []

  def test_a_task_is_queued_behind_the_others(self, lane):
    lane.add(done, key=("first",), scope="worker:w1")
    lane.add(done, key=("second",), scope="worker:w2")
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["first", "second"]

  @pytest.mark.parametrize("on_conflict", [OnConflict.REPLACE, OnConflict.REVISE])
  def test_a_replacement_is_queued_behind_the_others_not_in_the_place_of_the_task_it_supersedes(self, lane, on_conflict):
    lane.add(done, key=("sync",), on_conflict=on_conflict)
    lane.add(done, key=("other",), scope="worker:w2")
    lane.add(done, key=("sync",), on_conflict=on_conflict)
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["other", "sync"]


class TestCancel:
  def test_it_cancels_the_pending_tasks_with_the_key_and_records_them(self, lane):
    for task_id in ("t1", "t2"):
      lane.add(done, key=("lifecycle", "c1"), name="deploy", task_id=task_id, on_conflict=OnConflict.QUEUE)
    lane.add(done, key=("lifecycle", "c2"), name="deploy", task_id="t3", scope="worker:w2")
    assert lane.queue.cancel(("lifecycle", "c1")) == ["t1", "t2"]
    assert [task["task_id"] for task in lane.queue.snapshot()["queued"]] == ["t3"]
    assert lane.recorded == [("deploy", "cancelled"), ("deploy", "cancelled")]
    assert lane.changes == 4

  async def test_it_leaves_a_running_task_with_the_key_to_finish(self, lane):
    work, release = holding()
    lane.add(work, key=("lifecycle", "c1"), name="deploy")
    lane.queue.dispatch_tick()
    changes = lane.changes
    assert lane.queue.cancel(("lifecycle", "c1")) == []
    assert (len(lane.queue.snapshot()["running"]), lane.recorded, lane.changes) == (1, [], changes)
    release.set()
    await lane.run()
    assert lane.recorded == [("deploy", "completed")]

  def test_a_quiet_task_it_cancels_leaves_no_record(self, lane):
    lane.add(done, key=("reconcile", "c1"), quiet=True)
    assert lane.queue.cancel(("reconcile", "c1")) != []
    assert (lane.queue.snapshot()["queued"], lane.recorded) == ([], [])

  def test_the_restore_is_never_cancelled(self, lane):
    lane.queue.request_restore(done, "restore", "platform")
    assert lane.queue.cancel(("restore",)) == [] and lane.queue.cancel(("restore",), name="restore") == []
    assert lane.queue.state == QueueState.RESTORING and len(lane.queue.snapshot()["queued"]) == 1

  def test_it_cancels_the_pending_tasks_at_or_below_a_scope_whatever_their_key(self, lane):
    lane.add(done, key=("lifecycle", "c1"), scope="worker:w1:container:c1", task_id="deploy")
    lane.add(done, key=("backup", "c1"), scopes={"worker:w1:container:c1:volume:v1", "settings:s3"}, task_id="backup")
    lane.add(done, key=("lifecycle", "c2"), scope="worker:w1:container:c2", task_id="other")
    lane.add(done, key=("setup", "w1"), scope="worker:w1", task_id="host")
    assert lane.queue.cancel(scope="worker:w1:container:c1") == ["deploy", "backup"]
    assert [task["task_id"] for task in lane.queue.snapshot()["queued"]] == ["other", "host"]

  def test_a_name_narrows_what_a_key_cancels(self, lane):
    lane.add(done, key=("lifecycle", "c1"), name="stop", task_id="stop")
    assert lane.queue.cancel(("lifecycle", "c1"), name="deploy") == []
    assert lane.queue.cancel(("lifecycle", "c1"), name="stop") == ["stop"]

  def test_a_cancel_names_a_key_or_a_scope(self, lane):
    with pytest.raises(ValueError):
      lane.queue.cancel(name="deploy")

  def test_cancelling_what_is_not_there_changes_nothing(self, lane):
    lane.add(done, key=("lifecycle", "c1"))
    assert lane.queue.cancel(("lifecycle", "c2")) == []
    assert (lane.recorded, lane.changes) == ([], 1)


class TestStates:
  async def test_a_paused_queue_starts_nothing_and_resuming_starts_what_waited(self, lane):
    lane.queue.pause()
    lane.add(done, key=("a",))
    lane.queue.dispatch_tick()
    assert lane.queue.state == QueueState.PAUSED
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["a"]
    lane.queue.resume()
    await lane.run()
    assert lane.recorded == [("a", "completed")]

  def test_pause_and_resume_change_nothing_from_the_wrong_state(self, lane):
    lane.queue.resume()
    assert lane.queue.state == QueueState.RUNNING
    lane.queue.pause()
    lane.queue.pause()
    lane.queue.resume()
    assert lane.queue.state == QueueState.RUNNING

  def test_stopping_records_the_waiting_tasks_as_cancelled_and_refuses_new_ones(self, lane):
    lane.add(done, key=("waiting",))
    lane.queue.stop()
    assert lane.queue.state == QueueState.STOPPING
    assert lane.recorded == [("waiting", "cancelled")]
    assert lane.add(done, key=("late",), scope="worker:w2").outcome == Outcome.REFUSED
    assert lane.recorded == [("waiting", "cancelled"), ("late", "cancelled")]
    assert lane.queue.snapshot()["queued"] == []

  async def test_stopping_leaves_a_running_task_to_finish(self, lane):
    work, release = holding()
    lane.add(work, key=("running",))
    lane.queue.dispatch_tick()
    lane.queue.stop()
    assert [task["name"] for task in lane.queue.snapshot()["running"]] == ["running"]
    release.set()
    while lane.queue.snapshot()["running"]:
      await asyncio.sleep(0)
    assert lane.recorded == [("running", "completed")]


class TestRestore:
  def restore(self, lane, task=done, **kwargs):
    return lane.queue.request_restore(task, "restore_platform", "platform", **kwargs)

  @pytest.mark.parametrize("state", [QueueState.PAUSED, QueueState.RESTORING, QueueState.STOPPING])
  def test_it_is_refused_without_a_record_unless_the_queue_is_running(self, lane, state):
    {QueueState.PAUSED: lane.queue.pause, QueueState.RESTORING: lambda: self.restore(lane, task_id="first"),
     QueueState.STOPPING: lane.queue.stop}[state]()
    queued, changes = lane.queue.snapshot()["queued"], lane.changes
    assert self.restore(lane, task_id="second") == Added(Outcome.REFUSED)
    assert (lane.queue.state, lane.queue.snapshot()["queued"], lane.recorded, lane.changes) == (state, queued, [], changes)

  def test_it_waits_last_as_a_task_without_scopes_and_the_queue_is_restoring(self, lane):
    lane.add(done, key=("waiting",))
    added = self.restore(lane, params={"s3_path": "base/backups/platform/x.db.enc"}, labels={"Backup": "x"},
                         task_id="r1")
    assert added == Added(Outcome.ACCEPTED, "r1") and lane.queue.state == QueueState.RESTORING
    waiting, restore = lane.queue.snapshot()["queued"]
    assert waiting["name"] == "waiting"
    assert {name: restore[name] for name in ("task_id", "name", "key", "scopes", "params", "labels", "executor", "quiet")} == {
        "task_id": "r1", "name": "restore_platform", "key": ["restore_platform"], "scopes": [],
        "params": {"s3_path": "base/backups/platform/x.db.enc"}, "labels": {"Backup": "x"}, "executor": "platform",
        "quiet": False}
    assert lane.queue.has_task(("restore_platform",)) and not lane.queue.has_task(("restore_platform",), running_only=True)
    assert lane.changes == 2

  @pytest.mark.parametrize("on_conflict", list(OnConflict))
  def test_while_restoring_every_request_is_refused_without_a_record_or_a_change(self, lane, on_conflict):
    lane.add(done, key=("sync", "d1"), quiet=True)
    self.restore(lane)
    queued, changes = lane.queue.snapshot()["queued"], lane.changes
    for quiet in (True, False):
      assert lane.add(done, key=("sync", "d1"), on_conflict=on_conflict, quiet=quiet) == Added(Outcome.REFUSED)
      assert lane.add(done, key=("other",), scope="worker:w2", on_conflict=on_conflict, quiet=quiet) == Added(Outcome.REFUSED)
    assert (lane.queue.snapshot()["queued"], lane.recorded, lane.changes) == (queued, [], changes)

  async def test_the_tasks_queued_before_it_finish_first_and_it_runs_alone_as_a_task(self, lane):
    order, seen = [], {}
    work, release = holding()

    async def first():
      order.append("first")
      await work()

    async def behind():
      order.append("behind")

    async def beside():
      order.append("beside")

    async def restore():
      snapshot = lane.queue.snapshot()
      seen.update(id=task_id.get(), pool=active_executor.get(), state=lane.queue.state,
                  running=[task["name"] for task in snapshot["running"]], queued=snapshot["queued"])
      order.append("restore")
      lane.queue.end_restore()

    lane.add(first, scope="worker:w1")
    lane.queue.dispatch_tick()
    lane.add(behind, scope="worker:w1:container:c1")
    lane.add(beside, scope="worker:w2")
    self.restore(lane, task=restore, task_id="r1")
    lane.queue.dispatch_tick()
    await asyncio.sleep(0)
    assert order == ["first", "beside"] and [task["name"] for task in lane.queue.snapshot()["queued"]] == [
        "behind", "restore_platform"]
    release.set()
    await lane.run()
    assert order == ["first", "beside", "behind", "restore"]
    assert seen == {"id": "r1", "pool": lane.pool, "state": QueueState.RESTORING, "running": ["restore_platform"],
                    "queued": []}
    assert lane.recorded[-1] == ("restore_platform", "completed") and lane.queue.state == QueueState.RUNNING

  async def test_a_failed_restore_is_recorded_failed_and_its_end_leaves_the_queue_running(self, lane):
    async def restore():
      try:
        raise TaskFailed()
      finally:
        lane.queue.end_restore()

    self.restore(lane, task=restore)
    await lane.run()
    assert lane.recorded == [("restore_platform", "failed")] and lane.queue.state == QueueState.RUNNING
    assert lane.add(done, key=("after",)).outcome == Outcome.ACCEPTED

  @pytest.mark.parametrize("state", [QueueState.RUNNING, QueueState.PAUSED, QueueState.STOPPING])
  def test_ending_it_outside_a_restore_changes_nothing(self, lane, state):
    {QueueState.RUNNING: lambda: None, QueueState.PAUSED: lane.queue.pause, QueueState.STOPPING: lane.queue.stop}[state]()
    changes = lane.changes
    lane.queue.end_restore()
    assert (lane.queue.state, lane.changes) == (state, changes)

  def test_stopping_cancels_a_waiting_restore_with_the_tasks_before_it(self, lane):
    lane.add(done, key=("waiting",))
    self.restore(lane)
    lane.queue.stop()
    assert lane.recorded == [("waiting", "cancelled"), ("restore_platform", "cancelled")]
    assert lane.queue.snapshot()["queued"] == [] and lane.queue.state == QueueState.STOPPING
    lane.queue.dispatch_tick()
    assert lane.queue.snapshot()["running"] == []


class TestQueries:
  async def test_snapshot_lists_the_running_and_the_queued_tasks_with_what_people_see(self, lane):
    work, release = holding()
    lane.add(work, key=("holds",))
    lane.add(done, key=("deploy", "c1"), scope="worker:w1", on_conflict=OnConflict.QUEUE, params={"container_id": "c1"},
             inputs={"snapshot": {"secret": "s"}}, labels={"Application": "shop / web", "Worker": "w1"}, quiet=True)
    lane.queue.dispatch_tick()
    snapshot = lane.queue.snapshot()
    assert [(t["name"], t["status"]) for t in snapshot["running"]] == [("holds", "running")]
    [waiting] = snapshot["queued"]
    assert {name: waiting[name] for name in ("name", "key", "labels", "params", "quiet", "status", "scopes")} == {
        "name": "deploy", "key": ["deploy", "c1"], "labels": {"Application": "shop / web", "Worker": "w1"},
        "params": {"container_id": "c1"}, "quiet": True, "status": "queued", "scopes": ["worker:w1"]}
    assert "secret" not in str(snapshot)
    release.set()
    await lane.run()

  def test_is_busy_covers_the_pending_and_the_conflicting_scopes(self, lane):
    lane.add(done, scope="worker:w1:container:c1")
    assert lane.queue.is_busy(frozenset({"worker:w1:container:c1"})) is True
    assert lane.queue.is_busy(frozenset({"worker:w1"})) is True
    assert lane.queue.is_busy(frozenset({"worker:w1:container:c2"})) is False

  async def test_is_busy_covers_a_running_task_until_it_finishes(self, lane):
    work, release = holding()
    lane.add(work, scope="worker:w1")
    lane.queue.dispatch_tick()
    assert lane.queue.snapshot()["queued"] == []
    assert lane.queue.is_busy(frozenset({"worker:w1"})) is True
    assert lane.queue.is_busy(frozenset({"worker:w2"})) is False
    release.set()
    await lane.run()
    assert lane.queue.is_busy(frozenset({"worker:w1"})) is False

  def test_has_task_finds_a_task_by_the_start_of_its_key(self, lane):
    lane.add(done, key=("sync_domain", "d1", "repair"))
    assert lane.queue.has_task(("sync_domain", "d1")) is True
    assert lane.queue.has_task(("sync_domain",)) is True
    assert lane.queue.has_task(("sync_domain", "d1", "repair")) is True
    assert lane.queue.has_task(("sync_domain", "d2")) is False
    assert lane.queue.has_task(("sync_domain", "d1", "repair", "more")) is False

  async def test_has_task_can_ask_for_a_started_task_only(self, lane):
    work, release = holding()
    lane.add(work, key=("backup_database",))
    assert lane.queue.has_task(("backup_database",), running_only=True) is False
    lane.queue.dispatch_tick()
    assert lane.queue.has_task(("backup_database",), running_only=True) is True
    release.set()
    await lane.run()
    assert lane.queue.has_task(("backup_database",)) is False


class TestChangeHook:
  def test_an_accepted_task_is_a_change_and_an_existing_one_is_not(self, lane):
    lane.add(done, key=("a",))
    assert lane.changes == 1
    lane.add(done, key=("a",))
    assert lane.changes == 1

  def test_a_request_that_makes_a_task_visible_is_a_change(self, lane):
    lane.add(done, key=("a",), quiet=True)
    lane.add(done, key=("a",))
    assert lane.changes == 2

  def test_a_replaced_task_is_a_change(self, lane):
    lane.add(done, key=("a",), on_conflict=OnConflict.REPLACE)
    lane.add(done, key=("a",), on_conflict=OnConflict.REPLACE)
    assert lane.changes == 2

  async def test_starting_and_finishing_are_changes_and_a_tick_that_starts_nothing_is_not(self, lane):
    lane.queue.dispatch_tick()
    assert lane.changes == 0
    lane.add(done)
    await lane.run()
    assert lane.changes == 3

  async def test_a_finished_task_is_recorded_and_gone_when_the_hook_is_called(self, lane):
    seen = []
    lane.queue._notify = lambda: seen.append((list(lane.recorded), lane.queue.snapshot()["running"]))
    lane.add(done, key=("a",))
    await lane.run()
    assert seen[-1] == ([("a", "completed")], [])

  def test_pausing_and_resuming_are_changes_and_doing_either_twice_is_not(self, lane):
    lane.queue.pause()
    lane.queue.pause()
    assert lane.changes == 1
    lane.queue.resume()
    lane.queue.resume()
    assert lane.changes == 2

  def test_a_restore_is_a_change_when_it_begins_and_when_it_ends(self, lane):
    lane.queue.request_restore(done, "restore", "platform")
    assert lane.changes == 1
    lane.queue.end_restore()
    assert lane.changes == 2

  def test_stopping_is_a_change_and_so_is_what_it_cancels(self, lane):
    lane.add(done, key=("waiting",))
    lane.queue.stop()
    assert lane.changes == 2
    lane.add(done, key=("late",), scope="worker:w2")
    assert lane.changes == 3

  async def test_a_hook_that_raises_breaks_nothing(self, lane, caplog):
    def broken():
      raise RuntimeError("subscriber failed")

    lane.queue._notify = broken
    assert lane.add(done, key=("a",)).outcome == Outcome.ACCEPTED
    with caplog.at_level(logging.ERROR):
      await lane.run()
    assert lane.recorded == [("a", "completed")]
    assert any("change hook failed" in record.getMessage() for record in caplog.records)
