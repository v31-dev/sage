import logging
from datetime import datetime

from services.db import Task

logger = logging.getLogger(__name__)


class QueueMixin:
  """Manager-facing surface for the operation queue."""

  def add_task(self, **params) -> bool:
    return self.task_queue.add_task(**params)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    return self.task_queue.is_busy(scopes)

  def cancel_all_tasks(self):
    self.task_queue.cancel_all()

  def dispatch_tick(self):
    self.task_queue.dispatch_tick()

  def task_snapshot(self) -> dict:
    """Running + queued tasks held in memory by the queue."""
    return self.task_queue.snapshot()

  def _persist_task(self, task, status: str):
    """TaskQueue persistence hook: record a finished task in the Task table.
    Running tasks are tracked in memory only; only completed/failed/cancelled
    reach the DB, each as a single row. Best-effort so a DB hiccup never breaks
    the operation."""
    try:
      Task.create(
          task_id=task.task_id,
          name=task.name,
          scopes=sorted(task.scopes),
          params=task.params,
          executor=task.executor,
          status=status,
          finished_at=datetime.now(),
      )
    except Exception as e:
      logger.error(f"Failed to persist task {task.task_id} ({status}): {e}")
