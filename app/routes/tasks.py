import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query
from peewee import SQL, NodeList

from services.db import Task
from services.logs import Logs
from services.manager import Manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def list_tasks():
  """Running and queued tasks (in-memory)."""
  return Manager().task_snapshot()


def utc(value: datetime | None) -> datetime | None:
  """A time from a query, as the naive UTC the database keeps."""
  if value is not None and value.tzinfo is not None:
    value = value.astimezone(timezone.utc).replace(tzinfo=None)
  return value


def in_scope(scope: str):
  """A task with the scope or one of its children (`app` matches `app:demo-web`)."""
  return NodeList((SQL("EXISTS (SELECT 1 FROM json_each("), Task.scopes,
                   SQL(") WHERE value = ? OR substr(value, 1, ?) = ?)", (scope, len(scope) + 1, f"{scope}:"))), glue="")


@router.get("/history")
def list_task_history(name: str = "", scope: str = "", executor: str = "", status: str = "",
                      from_ts: datetime | None = None, to_ts: datetime | None = None,
                      limit: Annotated[int, Query(ge=1, le=500)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
  """Finished tasks (DB), newest finished first: a page of them and how many match. A task matches the window when
  it was active at any point in it, from queued to finished."""
  query = Task.select().where(Task.status != "running")
  if name:
    query = query.where(Task.name == name)
  if scope:
    query = query.where(in_scope(scope))
  if executor:
    query = query.where(Task.executor == executor)
  if status:
    query = query.where(Task.status == status)
  if from_ts:
    query = query.where(Task.finished_at >= utc(from_ts))
  if to_ts:
    query = query.where(Task.created_at <= utc(to_ts))
  page = query.order_by(Task.finished_at.desc(), Task.task_id.desc()).limit(limit).offset(offset)
  return {"tasks": list(page.dicts()), "total": query.count()}


@router.get("/history/filters")
def task_history_filters():
  """The choices of the history's filters: the names and executors stored, and the statuses."""
  def stored(field):
    return [value for (value,) in Task.select(field).distinct().order_by(field).tuples()]

  return {"names": stored(Task.name), "executors": stored(Task.executor), "statuses": list(Task.status.choices)}


@router.get("/{task_id}/logs")
def get_task_logs(task_id: str, sub_task_id: str = "", after_id: int = 0):
  """The lines a task logged, or one container's part of it (`sub_task_id`); after the line `after_id`, if given."""
  return Logs().query_task_logs(task_id, sub_task_id, after_id)
