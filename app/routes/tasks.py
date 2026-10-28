import logging

from fastapi import APIRouter

from services.db import Task
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
