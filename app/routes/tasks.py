import logging

from fastapi import APIRouter

from services.db import Task
from services.manager import Manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def list_tasks(limit: int = 100):
  """Running and queued tasks (in-memory) plus recent terminal tasks (DB)."""
  snapshot = Manager().task_snapshot()
  completed = list(
      Task.select()
      .where(Task.status != "running")
      .order_by(Task.created_at.desc())
      .limit(limit)
      .dicts()
  )
  return {
      "running": snapshot["running"],
      "queued": snapshot["queued"],
      "completed": completed,
  }
