import logging

from services.history import record_task, task_history_filters
from services.updates import Fact, UpdateFeed, Updates, UpdateSource
from utils.executor import LANES
from utils.queue import Added

from .lifecycle import REQUESTS
from .settings import SETTING_SECTIONS

logger = logging.getLogger(__name__)

SCOPES = frozenset({"worker", "domain", "certificate", "backup", "discovery", "settings", "metrics"})
# Every task's name: a task of another name is refused, and the history's filter offers each.
TASK_NAMES = frozenset({
    *REQUESTS, "backup_preserve", "recover", "reset", "read_record", "list_records", "observe_containers", "cleanup_orphan",
    "apply_domain", "reconcile_domains", "publish_discovery", "publish_discovery_dns",
    "observe_workers", "converge_worker", "repair_worker_stack", "sync_traefik_networks", "attach_traefik_networks",
    "cleanup_worker_tmp", "add_worker", "remove_worker", "reboot_worker", "refresh_worker_paths", "list_tunnels",
    "renew_certificates", "distribute_certificates",
    "discover_backups", "prune_backups", "delete_archive", "delete_volume_archives", "backup_platform", "restore_platform",
    "flush_metrics", "metrics_cleanup", "logs_cleanup", "purge_history", "safety_copies_cleanup", "refresh_latest_release",
    "send_summary_notification", *(f"{kind}_{section}" for kind in ("check", "test") for section in SETTING_SECTIONS),
})


class QueueMixin:
  """Manager-facing surface for the operation queue."""

  def add_task(self, **params) -> Added:
    return self.task_queue.add_task(**params)

  def task_history_filters(self) -> dict:
    """The choices of the history's filters: every task name and executor lane, with those stored, and the statuses."""
    return task_history_filters(TASK_NAMES, LANES)

  def cancel_tasks(self, key: tuple[str, ...] | None = None, name: str | None = None, scope: str | None = None) -> list[str]:
    return self.task_queue.cancel(key, name, scope)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    return self.task_queue.is_busy(scopes)

  def has_task(self, prefix: tuple[str, ...], running_only: bool = False) -> bool:
    return self.task_queue.has_task(prefix, running_only)

  def pause_tasks(self):
    self.task_queue.pause()

  def resume_tasks(self):
    self.task_queue.resume()

  def add_restore(self, **params) -> Added:
    return self.task_queue.request_restore(**params)

  def end_restore(self):
    self.task_queue.end_restore()

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
