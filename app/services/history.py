import logging
from datetime import datetime, timedelta, timezone

from peewee import SQL, NodeList
from playhouse.shortcuts import model_to_dict

import config
from services.db.database import HISTORY
from services.db.models import Container, ContainerEvent, Task, Worker, WorkerEvent
from services.logs import Logs
from utils.common import utcnow
from utils.db import like_literal
from utils.logging import sub_task_id, task_id
from utils.queue import QueuedTask
from values.names import qualified_name

logger = logging.getLogger(__name__)


def record_container_event(container: Container, action: str, transition: str, operation_id: str | None = None,
                           reason: dict | None = None, dedupe_key: str | None = None) -> bool:
  """Appends what happened to the container, with the running task and part; False when an event with `dedupe_key` is
  already recorded."""
  application = container.application
  return bool(ContainerEvent.insert(
      container=container.id, subject_id=container.id, action=action, transition=transition,
      operation_id=operation_id, reason=reason, task_id=task_id.get() or None, part_id=sub_task_id.get() or None,
      dedupe_key=dedupe_key, application_name=qualified_name(application.project_id, application.name),
      worker_hostname=container.worker_id,
  ).on_conflict_ignore().as_rowcount().execute())


def record_worker_event(hostname: str, action: str, transition: str, operation_id: str | None = None,
                        reason: dict | None = None, dedupe_key: str | None = None) -> bool:
  """Appends what happened to the worker, linked to its row when it has one; False when an event with `dedupe_key` is
  already recorded."""
  return bool(WorkerEvent.insert(
      worker=Worker.select(Worker.hostname).where(Worker.hostname == hostname).scalar(), subject_id=hostname,
      action=action, transition=transition, operation_id=operation_id, reason=reason,
      task_id=task_id.get() or None, part_id=sub_task_id.get() or None, dedupe_key=dedupe_key,
  ).on_conflict_ignore().as_rowcount().execute())


def task_parts(task_id: str) -> list[dict]:
  """The parts of a task that logged lines, in the order of their first line, each with the action and worker of its
  first container event (None for a part with none)."""
  events = {}
  for part, action, worker in (ContainerEvent.select(ContainerEvent.part_id, ContainerEvent.action, ContainerEvent.worker_hostname)
                               .where(ContainerEvent.task_id == task_id).order_by(ContainerEvent.id).tuples()):
    events.setdefault(part, {"type": action, "worker": worker})
  return [{"id": part, **events.get(part, {"type": None, "worker": None})} for part in Logs().query_task_parts(task_id)]


def purge_history(days: int = config.RETENTION_DAYS) -> int:
  """Deletes the history (container and worker events, notifications, tasks) older than `days`, and returns how many
  rows."""
  before = utcnow() - timedelta(days=days)
  removed = sum(model.delete().where(model.created_at < before).execute() for model in HISTORY)
  logger.info(f"Removed {removed} history rows older than {days} days.")
  return removed


def record_task(task: QueuedTask, status: str) -> dict:
  """Stores a task that reached `status` (completed, failed or cancelled) and returns its row. Running and queued tasks
  are in the queue's memory only."""
  row = Task.create(
      task_id=task.task_id,
      name=task.name,
      scopes=sorted(task.scopes),
      params=task.params,
      labels=task.labels,
      executor=task.executor,
      status=status,
      created_at=task.queued_at,
      started_at=task.started_at,
      finished_at=utcnow(),
  )
  return model_to_dict(row, recurse=False)


def _naive_utc(value: datetime) -> datetime:
  """A time as the naive UTC the database keeps."""
  return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def _in_scope(scope: str):
  """A task with a scope that matches `scope` or is one of its children (`app` matches `app:demo-web`), where `*`
  matches any characters and case does not matter (`app:test*` matches `app:test-nginx`)."""
  pattern = "%".join(like_literal(part) for part in scope.split("*"))
  return NodeList((SQL("EXISTS (SELECT 1 FROM json_each("), Task.scopes,
                   SQL(") WHERE value LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')", (pattern, f"{pattern}:%"))), glue="")


def _labelled(text: str):
  """A task with a label that holds `text`, in any case (`web` matches the application `shop / web`)."""
  return NodeList((SQL("EXISTS (SELECT 1 FROM json_each("), Task.labels,
                   SQL(") WHERE value LIKE ? ESCAPE '\\')", (f"%{like_literal(text)}%",))), glue="")


def task_history(name: str = "", scope: str = "", label: str = "", executor: str = "", status: str = "",
                 from_ts: datetime | None = None, to_ts: datetime | None = None,
                 limit: int = config.LIST_MAX_ROWS) -> dict:
  """The finished tasks `record_task` stored, newest finished first: the newest `limit` and how many match. A name,
  an executor and a status match exactly, a scope as `_in_scope` says, a label as `_labelled` says, and a window
  holds a task active at any point in it, from queued to finished."""
  query = Task.select()
  if name:
    query = query.where(Task.name == name)
  if scope:
    query = query.where(_in_scope(scope))
  if label:
    query = query.where(_labelled(label))
  if executor:
    query = query.where(Task.executor == executor)
  if status:
    query = query.where(Task.status == status)
  if from_ts:
    query = query.where(Task.finished_at >= _naive_utc(from_ts))
  if to_ts:
    query = query.where(Task.created_at <= _naive_utc(to_ts))
  newest = query.order_by(Task.finished_at.desc(), Task.task_id.desc()).limit(limit)
  return {"tasks": list(newest.dicts()), "total": query.count()}


def task_history_filters() -> dict:
  """The choices of the history's filters: the names and executors stored, and the statuses."""
  def stored(field):
    return [value for (value,) in Task.select(field).distinct().order_by(field).tuples()]

  return {"names": stored(Task.name), "executors": stored(Task.executor), "statuses": list(Task.status.choices)}
