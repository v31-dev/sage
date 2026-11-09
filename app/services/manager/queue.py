import logging

from services.history import record_task
from services.updates import Fact, UpdateFeed, Updates, UpdateSource
from utils.queue import Added

logger = logging.getLogger(__name__)

SCOPES = frozenset({"worker", "domain", "certificate", "backup", "discovery", "settings", "release", "metrics"})


class QueueMixin:
  """Manager-facing surface for the operation queue."""

  def add_task(self, **params) -> Added:
    return self.task_queue.add_task(**params)

  def cancel_tasks(self, key: tuple[str, ...]) -> list[str]:
    return self.task_queue.cancel(key)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    return self.task_queue.is_busy(scopes)

  def has_task(self, prefix: tuple[str, ...], running_only: bool = False) -> bool:
    return self.task_queue.has_task(prefix, running_only)

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
    Best-effort so a DB hiccup never breaks the operation."""
    try:
      row = record_task(task, status)
    except Exception as e:
      logger.error(f"Failed to persist task {task.task_id} ({status}): {e}")
    else:
      Updates().publish(UpdateFeed.TASK_HISTORY, [row])
