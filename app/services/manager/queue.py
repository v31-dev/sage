import logging
from datetime import datetime, timezone
from typing import Callable

from peewee import SQL, NodeList
from playhouse.shortcuts import model_to_dict

from services.db import Task
from services.updates import Fact, UpdateFeed, Updates, UpdateSource
from utils.common import utcnow
from utils.db import like_literal

logger = logging.getLogger(__name__)


class QueueMixin:
  """Manager-facing surface for the operation queue."""

  def add_task(self, **params) -> bool:
    return self.task_queue.add_task(**params)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    return self.task_queue.is_busy(scopes)

  def is_task_running(self, task: Callable | str, scopes: frozenset[str] | None = None) -> bool:
    return self.task_queue.is_task_running(task, scopes)

  def has_task(self, task: Callable | str, params: dict | None = None) -> bool:
    return self.task_queue.has_task(task, params)

  def pause_tasks(self):
    self.task_queue.pause()

  def resume_tasks(self):
    self.task_queue.resume()

  def end_restore(self, restored: bool, follow_ups: list[dict] = ()):
    self.task_queue.end_restore(restored, follow_ups)

  def stop_tasks(self):
    self.task_queue.stop()

  def task_state(self) -> str:
    """running, paused, restoring or stopping."""
    return self.task_queue.state.value

  def dispatch_tick(self):
    self.task_queue.dispatch_tick()

  def task_snapshot(self) -> dict:
    """Running + queued tasks held in memory by the queue."""
    return self.task_queue.snapshot()

  def tasks_queue_facts(self) -> list[Fact]:
    """What the queue itself holds that topics show: its tasks and its state."""
    return [Fact(self.task_snapshot, UpdateSource.TASKS), Fact(self.task_state, UpdateSource.QUEUE_STATE)]

  def _queue_changed(self):
    """TaskQueue change hook, called from any thread after the tasks or the state changed. Each owner's facts that come
    from the queue (`queue_facts`) are checked."""
    for fact in self.queue_facts:
      fact.check()

  def _persist_task(self, task, status: str):
    """TaskQueue persistence hook: record a finished task in the Task table.
    Running tasks are tracked in memory only; only completed/failed/cancelled
    reach the DB, each as a single row. Best-effort so a DB hiccup never breaks
    the operation."""
    try:
      row = Task.create(
          task_id=task.task_id,
          name=task.name,
          scopes=sorted(task.scopes),
          params=task.params,
          executor=task.executor,
          status=status,
          created_at=task.queued_at,
          started_at=task.started_at,
          finished_at=utcnow(),
      )
    except Exception as e:
      logger.error(f"Failed to persist task {task.task_id} ({status}): {e}")
    else:
      Updates().publish(UpdateFeed.TASK_HISTORY, [model_to_dict(row, recurse=False)])


def _naive_utc(value: datetime) -> datetime:
  """A time as the naive UTC the database keeps."""
  return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def _in_scope(scope: str):
  """A task with a scope that matches `scope` or is one of its children (`app` matches `app:demo-web`), where `*`
  matches any characters and case does not matter (`app:test*` matches `app:test-nginx`)."""
  pattern = "%".join(like_literal(part) for part in scope.split("*"))
  return NodeList((SQL("EXISTS (SELECT 1 FROM json_each("), Task.scopes,
                   SQL(") WHERE value LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')", (pattern, f"{pattern}:%"))), glue="")


def task_history(name: str = "", scope: str = "", executor: str = "", status: str = "", from_ts: datetime | None = None,
                 to_ts: datetime | None = None, limit: int = 50, offset: int = 0) -> dict:
  """The finished tasks `_persist_task` stored, newest finished first: a page of them and how many match. A name, an
  executor and a status match exactly, a scope as `_in_scope` says, and a window holds a task active at any point in
  it, from queued to finished."""
  query = Task.select().where(Task.status != "running")
  if name:
    query = query.where(Task.name == name)
  if scope:
    query = query.where(_in_scope(scope))
  if executor:
    query = query.where(Task.executor == executor)
  if status:
    query = query.where(Task.status == status)
  if from_ts:
    query = query.where(Task.finished_at >= _naive_utc(from_ts))
  if to_ts:
    query = query.where(Task.created_at <= _naive_utc(to_ts))
  page = query.order_by(Task.finished_at.desc(), Task.task_id.desc()).limit(limit).offset(offset)
  return {"tasks": list(page.dicts()), "total": query.count()}


def task_history_filters() -> dict:
  """The choices of the history's filters: the names and executors stored, and the statuses."""
  def stored(field):
    return [value for (value,) in Task.select(field).distinct().order_by(field).tuples()]

  return {"names": stored(Task.name), "executors": stored(Task.executor), "statuses": list(Task.status.choices)}
