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

  def dispatch_tick(self):
    self.task_queue.dispatch_tick()

  def task_snapshot(self) -> dict:
    """Running + queued tasks held in memory by the queue."""
    return self.task_queue.snapshot()

  def _persist_task(self, task, status: str):
    """TaskQueue persistence hook: log tasks to the Task table. Best-effort so a
    DB hiccup never breaks the operation. A row is created on dispatch (running)
    and on cancellation; completed/failed update the existing row."""
    try:
      now = datetime.now()
      if status in ("running", "cancelled"):
        Task.create(
            task_id=task.task_id,
            name=task.name,
            scopes=sorted(task.scopes),
            params=task.params,
            executor=task.executor,
            status=status,
            finished_at=now if status == "cancelled" else None,
        )
      else:  # completed / failed
        (Task.update(status=status, finished_at=now, updated_at=now)
         .where(Task.task_id == task.task_id).execute())
    except Exception as e:
      logger.error(f"Failed to persist task {task.task_id} ({status}): {e}")
