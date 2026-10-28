import logging

from services.db import Task
from services.manager.queue import QueueMixin
from utils.queue import QueuedTask


class Harness(QueueMixin):
  pass


def finished(**overrides):
  fields = dict(name="deploy", scopes=frozenset({"platform", "app:demo-web"}), task=lambda: None, params={"application_id": 7},
                executor="app", task_id="t-1")
  return QueuedTask(**{**fields, **overrides})


class TestPersistTask:
  def test_a_finished_task_is_recorded_with_what_identifies_it(self, db):
    Harness()._persist_task(finished(), "failed")
    row = Task.get_by_id("t-1")
    assert (row.name, row.status, row.executor) == ("deploy", "failed", "app")
    assert row.scopes == ["app:demo-web", "platform"]
    assert row.params == {"application_id": 7}
    assert row.finished_at is not None

  def test_a_database_error_does_not_break_the_operation(self, db, monkeypatch, caplog):
    def fails(**fields):
      raise RuntimeError("database is locked")

    monkeypatch.setattr(Task, "create", fails)
    with caplog.at_level(logging.ERROR):
      Harness()._persist_task(finished(), "completed")
    assert any("t-1" in record.getMessage() and "database is locked" in record.getMessage() for record in caplog.records)
