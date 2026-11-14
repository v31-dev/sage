import logging
from datetime import datetime

import pytest

from services.db.models import Task
from services.history import task_history
from services.manager.queue import QueueMixin
from services.updates import Fact, UpdateFeed, Updates, UpdateSource
from tests.conftest import Signals
from utils.queue import QueuedTask


@pytest.fixture
def hub(register):
  return register(Updates, Signals())


class TestQueueFacts:
  class Queue(QueueMixin):
    """What the queue holds that topics show, which the tests change."""

    def __init__(self):
      self.snapshot = {"running": [], "queued": []}
      self.state = "running"
      self.queue_facts = self.tasks_queue_facts()

    def task_snapshot(self):
      return {name: list(tasks) for name, tasks in self.snapshot.items()}

    def task_state(self):
      return self.state

  @pytest.fixture
  def queue(self, hub):
    queue = self.Queue()
    queue._queue_changed()
    hub.signals.clear()
    return queue

  def test_the_queue_shows_its_tasks_and_its_state(self):
    queue = self.Queue()
    queue.state = "paused"
    queue.snapshot["running"].append({"task_id": "t1"})
    values = {fact._source: fact._compute() for fact in queue.tasks_queue_facts()}
    assert values == {UpdateSource.TASKS: {"running": [{"task_id": "t1"}], "queued": []},
                      UpdateSource.QUEUE_STATE: "paused"}

  def test_both_are_shown_the_first_time(self, hub):
    self.Queue()._queue_changed()
    assert sorted(source for source, _ in hub.signals) == [UpdateSource.QUEUE_STATE, UpdateSource.TASKS]

  def test_a_change_that_nothing_shows_is_not_a_change(self, queue, hub):
    queue._queue_changed()
    assert hub.signals == []

  def test_a_task_that_starts_and_ends_changes_the_tasks_only(self, queue, hub):
    queue.snapshot["running"].append({"task_id": "t1", "name": "deploy"})
    queue._queue_changed()
    queue.snapshot["running"].clear()
    queue._queue_changed()
    assert hub.signals == [(UpdateSource.TASKS, None), (UpdateSource.TASKS, None)]

  def test_the_state_changing_changes_the_state_only(self, queue, hub):
    queue.state = "paused"
    queue._queue_changed()
    assert hub.signals == [(UpdateSource.QUEUE_STATE, None)]

  def test_every_fact_the_queue_is_given_is_checked(self, queue, hub):
    seen = []
    queue.queue_facts.append(Fact(lambda: seen.append("checked") or len(seen), UpdateSource.RESTORING))
    queue._queue_changed()
    assert seen == ["checked"]
    assert hub.signals == [(UpdateSource.RESTORING, None)]


def finished(**fields):
  return QueuedTask(**{"name": "deploy", "key": ("lifecycle", "c1"), "scopes": frozenset({"worker:w1:container:c1"}),
                       "task": lambda: None, "params": {"container_id": "c1"}, "executor": "app", "task_id": "t-1", **fields})


class TestPersistTask:
  def test_a_finished_task_is_published_to_the_history_once_it_is_stored(self, db, hub):
    seen = []
    hub.publish = lambda feed, rows, key=None: seen.append((feed, rows, Task.select().count()))
    QueueMixin()._persist_task(finished(), "failed")
    [(feed, [row], stored)] = seen
    assert (feed, stored, row["task_id"], row["status"]) == (UpdateFeed.TASK_HISTORY, 1, "t-1", "failed")

  def test_the_published_row_is_the_row_the_history_returns(self, db, hub, clock):
    QueueMixin()._persist_task(finished(queued_at=datetime(2026, 1, 1, 11, 58), started_at=datetime(2026, 1, 1, 11, 59)),
                               "completed")
    [(_, rows, _)] = hub.published
    assert rows == task_history()["tasks"]

  def test_a_task_that_could_not_be_stored_is_logged_and_not_published(self, db, hub, monkeypatch, caplog):
    def fails(**fields):
      raise RuntimeError("database is locked")

    monkeypatch.setattr(Task, "create", fails)
    with caplog.at_level(logging.ERROR):
      QueueMixin()._persist_task(finished(), "completed")
    assert any("t-1" in record.getMessage() and "database is locked" in record.getMessage() for record in caplog.records)
    assert hub.published == []

  def test_a_finished_task_is_not_a_change_of_a_source(self, db, hub):
    QueueMixin()._persist_task(finished(), "completed")
    assert hub.signals == []
