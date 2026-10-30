import logging
from typing import Callable

from playhouse.shortcuts import model_to_dict

from services.db import Task
from services.updates import Fact, UpdateFeed, Updates, UpdateSource
from utils.common import utcnow

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
