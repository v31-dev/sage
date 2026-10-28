import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils.executor import active_executor
from utils.logging import TaskFailed, task_id
from utils.queue import Admit, OnConflict, QueuedTask, QueueState, TaskQueue, admit, next_to_start


def queued(name, *scopes, restore=False):
  return QueuedTask(name=name, scopes=frozenset(scopes), task=lambda: None, params={}, executor="app",
                    task_id=f"{name}-{'-'.join(sorted(scopes))}", restore=restore)


def admission(new, *, state=QueueState.RUNNING, running=(), queue=(), on_conflict=OnConflict.DEDUP,
              restore=False, priority=False):
  return admit(state, list(running), list(queue), new, on_conflict, restore, priority)


class TestAdmit:
  def test_a_task_on_an_empty_queue_is_enqueued_first(self):
    result = admission(queued("deploy", "app:a"))
    assert (result.admit, result.superseded, result.position) == (Admit.ENQUEUE, (), 0)

  def test_it_goes_to_the_end_of_the_queue(self):
    result = admission(queued("deploy", "app:a"), queue=[queued("x", "app:b"), queued("y", "app:c")])
    assert result.position == 2

  @pytest.mark.parametrize("where", ["running", "queue"])
  def test_dedup_drops_an_identical_task_that_is_pending_or_running(self, where):
    same = queued("sync", "app:a")
    result = admission(queued("sync", "app:a"), **{where: [same]})
    assert result.admit == Admit.DROP

  @pytest.mark.parametrize("other", [queued("sync", "app:b"), queued("backup", "app:a"), queued("sync", "app")])
  def test_dedup_keeps_a_task_that_differs_in_name_or_exact_scope(self, other):
    assert admission(queued("sync", "app:a"), queue=[other]).admit == Admit.ENQUEUE

  @pytest.mark.parametrize("where", ["running", "queue"])
  def test_queue_enqueues_a_duplicate(self, where):
    result = admission(queued("sync", "app:a"), on_conflict=OnConflict.QUEUE, **{where: [queued("sync", "app:a")]})
    assert (result.admit, result.superseded) == (Admit.ENQUEUE, ())

  def test_replace_supersedes_the_pending_duplicate_and_takes_the_end(self):
    old = queued("sync", "app:a")
    result = admission(queued("sync", "app:a"), on_conflict=OnConflict.REPLACE,
                       queue=[old, queued("x", "app:b")])
    assert (result.admit, result.superseded, result.position) == (Admit.ENQUEUE, (old,), 1)

  def test_replace_leaves_a_running_duplicate_alone(self):
    result = admission(queued("sync", "app:a"), on_conflict=OnConflict.REPLACE, running=[queued("sync", "app:a")])
    assert (result.admit, result.superseded) == (Admit.ENQUEUE, ())

  def test_replace_does_not_touch_the_same_name_on_another_scope(self):
    result = admission(queued("sync", "app:a"), on_conflict=OnConflict.REPLACE, queue=[queued("sync", "app:b")])
    assert result.superseded == ()

  def test_a_priority_task_goes_behind_the_tasks_that_cover_it_and_ahead_of_the_rest(self):
    queue = [queued("p1", "platform"), queued("p2", "platform"), queued("a", "app:x")]
    assert admission(queued("p3", "platform"), queue=queue, priority=True).position == 2

  def test_a_priority_app_task_goes_behind_the_app_wide_task_only(self):
    queue = [queued("all", "app"), queued("a", "app:x")]
    assert admission(queued("b", "app:y"), queue=queue, priority=True).position == 1

  def test_a_priority_task_that_nothing_covers_goes_first(self):
    assert admission(queued("a", "app:y"), queue=[queued("p", "platform")], priority=True).position == 0

  def test_a_priority_task_is_placed_after_its_duplicates_are_superseded(self):
    queue = [queued("p1", "platform"), queued("sync", "platform"), queued("a", "app:x")]
    result = admission(queued("sync", "platform"), queue=queue, on_conflict=OnConflict.REPLACE, priority=True)
    assert (len(result.superseded), result.position) == (1, 1)

  @pytest.mark.parametrize("state", [QueueState.PAUSED, QueueState.RESTORING])
  def test_a_task_is_enqueued_while_paused_or_restoring(self, state):
    assert admission(queued("a", "app:x"), state=state).admit == Admit.ENQUEUE

  def test_dedup_still_keeps_one_pending_copy_while_paused(self):
    result = admission(queued("sync", "app:a"), state=QueueState.PAUSED, queue=[queued("sync", "app:a")])
    assert result.admit == Admit.DROP

  def test_a_stopping_queue_cancels_what_is_added(self):
    assert admission(queued("a", "app:x"), state=QueueState.STOPPING).admit == Admit.CANCEL

  def test_a_restore_is_enqueued_only_while_running(self):
    assert admission(queued("restore", "platform"), restore=True).admit == Admit.ENQUEUE

  @pytest.mark.parametrize("state", [QueueState.PAUSED, QueueState.RESTORING, QueueState.STOPPING])
  def test_a_restore_is_dropped_in_any_other_state(self, state):
    assert admission(queued("restore", "platform"), state=state, restore=True).admit == Admit.DROP


class TestNextToStart:
  def names(self, state, queue, running=()):
    return [task.name for task in next_to_start(state, list(queue), list(running))]

  def test_tasks_on_sibling_scopes_start_together(self):
    queue = [queued("a", "app:x"), queued("b", "app:y"), queued("c", "common")]
    assert self.names(QueueState.RUNNING, queue) == ["a", "b", "c"]

  def test_a_task_on_a_scope_that_is_running_waits(self):
    queue = [queued("b", "app:x"), queued("c", "app:y")]
    assert self.names(QueueState.RUNNING, queue, running=[queued("a", "app:x")]) == ["c"]

  def test_the_second_of_two_tasks_on_one_scope_waits_for_the_first_started_in_the_same_scan(self):
    queue = [queued("a", "app:x"), queued("b", "app:x")]
    assert self.names(QueueState.RUNNING, queue) == ["a"]

  def test_a_parent_scope_waits_for_a_running_child_without_holding_back_the_children_after_it(self):
    queue = [queued("all", "app"), queued("b", "app:y")]
    assert self.names(QueueState.RUNNING, queue, running=[queued("a", "app:x")]) == ["b"]

  def test_a_parent_scope_that_starts_holds_back_the_children_after_it(self):
    queue = [queued("all", "app"), queued("b", "app:y")]
    assert self.names(QueueState.RUNNING, queue) == ["all"]

  def test_a_child_started_earlier_in_the_scan_holds_the_parent_scope_back(self):
    queue = [queued("a", "app:x"), queued("all", "app"), queued("c", "platform")]
    assert self.names(QueueState.RUNNING, queue) == ["a", "c"]

  def test_scopes_that_only_share_a_prefix_do_not_conflict(self):
    queue = [queued("a", "app:p-a1"), queued("b", "app:p-a10"), queued("c", "app:p-a")]
    assert self.names(QueueState.RUNNING, queue) == ["a", "b", "c"]

  def test_a_task_holding_several_scopes_conflicts_on_any_of_them(self):
    queue = [queued("both", "platform", "app")]
    assert self.names(QueueState.RUNNING, queue, running=[queued("a", "app:x")]) == []
    assert self.names(QueueState.RUNNING, queue, running=[queued("p", "common")]) == ["both"]

  def test_while_restoring_only_the_restore_starts(self):
    queue = [queued("a", "app:x"), queued("restore", "platform", restore=True), queued("b", "app:y")]
    assert self.names(QueueState.RESTORING, queue) == ["restore"]

  def test_while_restoring_the_restore_waits_for_the_running_tasks_it_conflicts_with(self):
    queue = [queued("restore", "platform", restore=True)]
    assert self.names(QueueState.RESTORING, queue, running=[queued("p", "platform")]) == []
    assert self.names(QueueState.RESTORING, queue, running=[queued("a", "app:x")]) == ["restore"]

  @pytest.mark.parametrize("state", [QueueState.PAUSED, QueueState.STOPPING])
  def test_nothing_starts_while_paused_or_stopping(self, state):
    assert self.names(state, [queued("a", "app:x")]) == []

  def test_an_empty_queue_starts_nothing(self):
    assert self.names(QueueState.RUNNING, []) == []


class Lane:
  """A queue over two pools with its records kept."""

  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lane")
    self.recorded = []
    self.changes = 0
    self.queue = TaskQueue(frozenset(["platform", "app"]), {"platform": self.pool, "app": self.pool},
                           record=lambda task, status: self.recorded.append((task.name, status)),
                           notify=self.changed)

  def changed(self):
    self.changes += 1

  def add(self, task, scope="app:a", **kwargs):
    kwargs.setdefault("executor", "app")
    return self.queue.add_task(task=task, scopes={scope}, **kwargs)

  async def run(self):
    """Start what can start and wait until nothing is running."""
    self.queue.dispatch_tick()
    while self.queue.snapshot()["running"]:
      await asyncio.sleep(0)
      self.queue.dispatch_tick()


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


class TestRunTask:
  async def test_a_task_that_completes_is_recorded_and_no_longer_running(self, lane):
    lane.add(done, name="deploy")
    await lane.run()
    assert lane.recorded == [("deploy", "completed")]
    assert lane.queue.snapshot() == {"running": [], "queued": []}

  async def test_a_quiet_task_is_recorded_only_when_it_fails(self, lane):
    lane.add(done, name="ok", quiet=True)
    lane.add(fails, name="bad", quiet=True, scope="app:b")
    await lane.run()
    assert lane.recorded == [("bad", "failed")]

  async def test_a_task_that_raises_task_failed_is_failed_without_a_traceback(self, lane, caplog):
    lane.add(fails, name="bad")
    with caplog.at_level(logging.INFO):
      await lane.run()
    assert lane.recorded == [("bad", "failed")]
    record = next(r for r in caplog.records if r.getMessage() == "bad: failed")
    assert record.levelno == logging.ERROR and record.exc_info is None

  async def test_a_task_that_raises_anything_else_is_failed_with_its_traceback(self, lane, caplog):
    lane.add(crashes, name="bad")
    with caplog.at_level(logging.INFO):
      await lane.run()
    assert lane.recorded == [("bad", "failed")]
    record = next(r for r in caplog.records if r.getMessage() == "bad: failed")
    assert record.exc_info is not None

  async def test_a_failed_task_frees_its_scope_for_the_next(self, lane):
    lane.add(crashes, name="bad")
    lane.add(done, name="next", on_conflict=OnConflict.QUEUE)
    await lane.run()
    assert lane.recorded == [("bad", "failed"), ("next", "completed")]

  async def test_a_task_cancelled_by_the_loop_shutting_down_is_failed(self, lane):
    started = asyncio.Event()

    async def hangs():
      started.set()
      await asyncio.sleep(3600)

    lane.add(hangs, name="hangs")
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

  async def test_a_sync_task_runs_on_its_lane_with_its_params(self, lane):
    seen = {}

    def work(value):
      seen["thread"] = threading.current_thread().name
      seen["value"] = value

    lane.add(work, params={"value": 7})
    await lane.run()
    assert seen["thread"].startswith("lane")
    assert seen["value"] == 7


class TestAddTask:
  def test_it_is_refused_for_an_unknown_scope_root_or_lane(self, lane):
    with pytest.raises(ValueError, match="Unknown scope root"):
      lane.queue.add_task(task=done, scopes={"nowhere:a"}, executor="app")
    with pytest.raises(ValueError, match="Unknown executor"):
      lane.queue.add_task(task=done, scopes={"app:a"}, executor="nowhere")

  def test_it_returns_whether_the_task_was_admitted(self, lane):
    assert lane.add(done, name="a") is True
    assert lane.add(done, name="a") is False
    assert lane.add(done, name="a", on_conflict=OnConflict.QUEUE) is True

  def test_a_replaced_pending_task_is_recorded_as_cancelled(self, lane):
    lane.add(done, name="sync", on_conflict=OnConflict.REPLACE)
    lane.add(done, name="sync", on_conflict=OnConflict.REPLACE)
    assert lane.recorded == [("sync", "cancelled")]
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["sync"]

  def test_a_quiet_task_that_is_replaced_leaves_no_record(self, lane):
    lane.add(done, name="sync", on_conflict=OnConflict.REPLACE, quiet=True)
    lane.add(done, name="sync", on_conflict=OnConflict.REPLACE, quiet=True)
    assert lane.recorded == []

  def test_a_priority_task_is_queued_ahead_of_the_others(self, lane):
    lane.add(done, name="first", scope="app:x")
    lane.add(done, name="urgent", scope="app:y", priority=True)
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["urgent", "first"]

  def test_a_restore_puts_the_queue_in_the_restoring_state(self, lane):
    assert lane.add(done, name="restore", scope="platform", executor="platform", restore=True) is True
    assert lane.queue.state == QueueState.RESTORING
    assert lane.add(done, name="restore", scope="platform", executor="platform", restore=True) is False


class TestStates:
  async def test_a_paused_queue_starts_nothing_and_resuming_starts_what_waited(self, lane):
    lane.queue.pause()
    lane.add(done, name="a")
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

  async def test_while_restoring_only_the_restore_runs(self, lane):
    order = []

    async def restore():
      order.append("restore")

    async def other():
      order.append("other")

    lane.add(other, name="other", scope="app:a")
    lane.add(restore, name="restore", scope="platform", executor="platform", restore=True)
    lane.queue.dispatch_tick()
    while lane.queue.snapshot()["running"]:
      await asyncio.sleep(0)
    assert order == ["restore"]

  def test_a_restore_that_took_effect_cancels_the_waiting_tasks_and_queues_the_follow_ups_first(self, lane):
    lane.add(done, name="restore", scope="platform", executor="platform", restore=True)
    lane.add(done, name="waiting", scope="app:a")
    lane.queue.end_restore(True, [dict(task=done, scopes={"app:b"}, executor="app", name="after")])
    assert lane.queue.state == QueueState.RUNNING
    assert lane.recorded == [("restore", "cancelled"), ("waiting", "cancelled")]
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["after"]

  def test_a_restore_that_failed_leaves_the_waiting_tasks(self, lane):
    lane.add(done, name="waiting", scope="app:a")
    lane.add(done, name="restore", scope="platform", executor="platform", restore=True)
    lane.queue.end_restore(False, [dict(task=done, scopes={"app:b"}, executor="app", name="after")])
    assert lane.queue.state == QueueState.RUNNING
    assert lane.recorded == []
    assert sorted(task["name"] for task in lane.queue.snapshot()["queued"]) == ["restore", "waiting"]

  def test_a_restore_that_ends_after_the_queue_stopped_leaves_it_stopped(self, lane):
    lane.add(done, name="restore", scope="platform", executor="platform", restore=True)
    lane.queue.stop()
    lane.queue.end_restore(True)
    assert lane.queue.state == QueueState.STOPPING

  def test_ending_a_restore_that_is_not_under_way_cancels_nothing(self, lane):
    lane.add(done, name="waiting", scope="app:a")
    lane.queue.end_restore(True)
    assert lane.queue.state == QueueState.RUNNING
    assert lane.recorded == []
    assert [task["name"] for task in lane.queue.snapshot()["queued"]] == ["waiting"]

  def test_stopping_records_the_waiting_tasks_as_cancelled_and_refuses_new_ones(self, lane):
    lane.add(done, name="waiting")
    lane.queue.stop()
    assert lane.queue.state == QueueState.STOPPING
    assert lane.recorded == [("waiting", "cancelled")]
    assert lane.add(done, name="late", scope="app:b") is False
    assert lane.recorded == [("waiting", "cancelled"), ("late", "cancelled")]
    assert lane.queue.snapshot()["queued"] == []

  async def test_stopping_leaves_a_running_task_to_finish(self, lane):
    release = asyncio.Event()

    async def finishes():
      await release.wait()

    lane.add(finishes, name="running")
    lane.queue.dispatch_tick()
    lane.queue.stop()
    assert [task["name"] for task in lane.queue.snapshot()["running"]] == ["running"]
    release.set()
    while lane.queue.snapshot()["running"]:
      await asyncio.sleep(0)
    assert lane.recorded == [("running", "completed")]


class TestQueries:
  async def test_snapshot_lists_the_running_and_the_queued_tasks(self, lane):
    release = asyncio.Event()

    async def holds():
      await release.wait()

    lane.add(holds, name="holds", scope="app:a")
    lane.add(done, name="waits", scope="app:a", on_conflict=OnConflict.QUEUE, params={"n": 2}, quiet=True)
    lane.queue.dispatch_tick()
    snapshot = lane.queue.snapshot()
    assert [(t["name"], t["status"]) for t in snapshot["running"]] == [("holds", "running")]
    assert [(t["name"], t["status"], t["params"], t["quiet"], t["scopes"]) for t in snapshot["queued"]] == [
        ("waits", "queued", {"n": 2}, True, ["app:a"])]
    release.set()
    await lane.run()

  def test_is_busy_covers_the_pending_and_the_conflicting_scopes(self, lane):
    lane.add(done, scope="app:a")
    assert lane.queue.is_busy(frozenset({"app:a"})) is True
    assert lane.queue.is_busy(frozenset({"app"})) is True
    assert lane.queue.is_busy(frozenset({"app:b"})) is False

  async def test_is_busy_covers_a_running_task_until_it_finishes(self, lane):
    release = asyncio.Event()

    async def holds():
      await release.wait()

    lane.add(holds, scope="app:a")
    lane.queue.dispatch_tick()
    assert lane.queue.snapshot()["queued"] == []
    assert lane.queue.is_busy(frozenset({"app:a"})) is True
    assert lane.queue.is_busy(frozenset({"app:b"})) is False
    release.set()
    await lane.run()
    assert lane.queue.is_busy(frozenset({"app:a"})) is False

  async def test_is_task_running_is_for_a_started_task_only(self, lane):
    release = asyncio.Event()

    async def holds():
      await release.wait()

    lane.add(holds)
    assert lane.queue.is_task_running(holds) is False
    lane.queue.dispatch_tick()
    assert lane.queue.is_task_running(holds) is True
    assert lane.queue.is_task_running("holds") is True
    assert lane.queue.is_task_running(holds, frozenset({"app:a"})) is True
    assert lane.queue.is_task_running(holds, frozenset({"app:b"})) is False
    release.set()
    await lane.run()

  def test_has_task_finds_a_queued_task_by_name_and_params(self, lane):
    lane.add(done, name="remove", params={"worker": "w1", "force": False}, on_conflict=OnConflict.QUEUE)
    assert lane.queue.has_task("remove") is True
    assert lane.queue.has_task(done) is False
    assert lane.queue.has_task("remove", {"worker": "w1"}) is True
    assert lane.queue.has_task("remove", {"worker": "w2"}) is False


class TestChangeHook:
  def test_an_admitted_task_is_a_change_and_a_dropped_one_is_not(self, lane):
    lane.add(done, name="a")
    assert lane.changes == 1
    lane.add(done, name="a")
    assert lane.changes == 1

  def test_a_replaced_task_is_a_change(self, lane):
    lane.add(done, name="a", on_conflict=OnConflict.REPLACE)
    lane.add(done, name="a", on_conflict=OnConflict.REPLACE)
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
    lane.add(done, name="a")
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
    lane.add(done, name="restore", scope="platform", executor="platform", restore=True)
    assert lane.changes == 1
    lane.queue.end_restore(False)
    assert lane.changes == 2

  def test_stopping_is_a_change_and_so_is_what_it_cancels(self, lane):
    lane.add(done, name="waiting")
    lane.queue.stop()
    assert lane.changes == 2
    lane.add(done, name="late", scope="app:b")
    assert lane.changes == 3

  async def test_a_hook_that_raises_breaks_nothing(self, lane, caplog):
    def broken():
      raise RuntimeError("subscriber failed")

    lane.queue._notify = broken
    assert lane.add(done, name="a") is True
    with caplog.at_level(logging.ERROR):
      await lane.run()
    assert lane.recorded == [("a", "completed")]
    assert any("change hook failed" in record.getMessage() for record in caplog.records)
