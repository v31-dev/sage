import logging

from fastapi import APIRouter

from services.db import Task
from services.logs import Logs
from services.manager import Manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def list_tasks():
  """Running and queued tasks (in-memory)."""
  return Manager().task_snapshot()


@router.get("/history")
def list_task_history(limit: int = 100):
  """Recent terminal tasks (DB), newest first."""
  return list(
      Task.select()
      .where(Task.status != "running")
      .order_by(Task.created_at.desc())
      .limit(limit)
      .dicts()
  )


@router.get("/{task_id}/logs")
def get_task_logs(task_id: str, sub_task_id: str = ""):
  """The lines a task logged, or one container's part of it (`sub_task_id`)."""
  return Logs().query_task_logs(task_id, sub_task_id)
