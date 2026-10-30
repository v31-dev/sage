from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

import config
from services.logs import Logs
from services.manager import Manager
from services.manager.application import task_parts
from services.manager.queue import task_history, task_history_filters
from utils.logging import NO_ACCESS_LOG

router = APIRouter()


@router.get("/")
def list_tasks():
  """Running and queued tasks (in-memory)."""
  return Manager().task_snapshot()


@router.get("/history")
def list_task_history(name: str = "", scope: str = "", executor: str = "", status: str = "",
                      from_ts: datetime | None = None, to_ts: datetime | None = None,
                      limit: Annotated[int, Query(ge=1, le=config.LIST_MAX_ROWS)] = config.LIST_MAX_ROWS):
  """Finished tasks, newest finished first: the newest `limit` that match, and how many match."""
  return task_history(name, scope, executor, status, from_ts, to_ts, limit)


@router.get("/history/filters")
def list_task_history_filters():
  return task_history_filters()


@router.get("/{task_id}/parts")
def get_task_parts(task_id: str):
  """The parts of a task that logged lines (one container's deploy, stop, delete, backup or restore each), in the
  order of their first line, with their kind and worker."""
  return task_parts(task_id)


@router.get("/{task_id}/logs", openapi_extra=NO_ACCESS_LOG)  # what a task log dialog loads
def get_task_logs(task_id: str, sub_task_id: str = "", after_id: int = 0):
  """Every line a task logged, or one container's part of it (`sub_task_id`); after the line `after_id`, if given."""
  return Logs().query_task_logs(task_id, sub_task_id, after_id)
