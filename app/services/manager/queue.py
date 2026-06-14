import logging

logger = logging.getLogger(__name__)


class QueueMixin:
  """Manager-facing surface for the operation queue."""

  def add_task(self, **params) -> bool:
    return self.task_queue.add_task(**params)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    return self.task_queue.is_busy(scopes)

  def dispatch_tick(self):
    self.task_queue.dispatch_tick()
