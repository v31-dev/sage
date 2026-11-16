"""The platform's backup, which keeps its configuration in the store, and its restore, which installs one as a restart
of the Manager would find it; what runs when Sage starts, at startup and after a restore; its daily cleanup and the
minutely write of the metrics; the check for a newer release; and the restore and backup facts the pages show."""
import logging
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from croniter import croniter

import config
from services.backups.catalog import CatalogPlan
from services.backups.commands import add_backups, apply_catalog, cancel_queued_backups, requeue_backups, settle_backups
from services.backups.decisions import SlotRow, platform_restore_refusal, slot_due
from services.backups.keys import (BACKUP_TIMESTAMP_FORMAT, PLATFORM_BACKUP_SUFFIX, PLATFORM_PREFIX, archive_name, archive_write_scope,
                                   platform_backup_key, taken_at, timestamp_from_key)
from services.containers.targets import Containers
from services.db import migrations
from services.db.database import Database
from services.db.migration_log import MigrationRuns
from services.db.models import Backup
from services.domains.state import Domains
from services.history import purge_history
from services.logs import Logs
from services.metrics import Metrics, metrics_scope
from services.notification import Notifications
from services.releases import Releases
from services.s3 import S3
from services.settings import Settings
from services.tailscale import Tailscale
from services.updates import Fact, UpdateSource
from services.workers.state import Workers
from utils.archive import decrypt_file, encrypt_file
from utils.common import get_env, utcnow
from utils.errors import Conflict
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed
from utils.queue import OnConflict, Outcome

from .backups import configured_session, settling

logger = logging.getLogger(__name__)


class PlatformMixin:
  def platform_queue_facts(self) -> list[Fact]:
    return [Fact(self.restoring_from, UpdateSource.RESTORING), Fact(self.backup_status, UpdateSource.BACKUP_STATUS)]

  def backup_status(self) -> dict:
    """Whether a platform backup is running and whether backups are being discovered."""
    return {"platform_backup_in_progress": self.has_task(("backup_platform",), running_only=True),
            "discovery_in_progress": self.has_task(("discover_backups",))}

  def info(self) -> dict:
    """What every page shows of the manager, from memory only: it is the health check and is served while paused or
    restoring. A settings status that cannot be built is none, with the error logged."""
    try:
      settings = self.settings_status()
    except Exception:
      logger.error("Could not build the settings status.", exc_info=True)
      settings = None
    return {
        "version": self.version,
        "latest_version": Releases().latest_version,
        "domain": get_env("DOMAIN"),
        "name": get_env("TS_NAME"),
        "ip": Tailscale().ip(),
        "start_time": self.started_at.isoformat(),
        "state": self.task_state(),
        "restoring_from": self.restoring_from(),
        "migration": MigrationRuns().latest(),
        "settings": settings,
    }

  def restoring_from(self) -> str | None:
    """When the backup being restored was taken, while restoring."""
    if self.task_state() != "restoring":
      return None
    snapshot = self.task_snapshot()
    for task in (*snapshot["running"], *snapshot["queued"]):
      if task["name"] == "restore_platform" and (timestamp := timestamp_from_key(task["key"][1])):
        return datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT).isoformat()
    return None

  def request_startup(self):
    """Requests what runs when Sage starts, at startup and after a platform restore: the workers' observation, whose
    setups follow, and their paths; the containers' observation; the certificates' renewal; the Domain pass, whose
    discovery follows; the backup discovery; the settings checks; the release check."""
    self.request_worker_observation()
    self.request_worker_paths()
    self.request_tunnel_listing()
    self.request_container_observation()
    self.request_certificate_renewal()
    self.request_domain_reconciliation()
    self.request_backup_discovery()
    self.request_settings_checks()
    self.request_release_check()

  def request_cleanup(self):
    """Requests the daily cleanup, each part a task of its own: the metrics, logs, history and an interrupted restore's
    safety copies past their retention, each worker's temp folder, and the store's old backups."""
    self.add_task(task=Metrics().cleanup, key=("metrics_cleanup",), scopes={metrics_scope()}, executor="metrics", quiet=True,
                  on_conflict=OnConflict.REPLACE)
    self.add_task(task=Logs().cleanup, key=("logs_cleanup",), scopes=set(), executor="common", quiet=True,
                  on_conflict=OnConflict.REPLACE)
    self.add_task(task=purge_history, key=("purge_history",), scopes=set(), executor="common", quiet=True,
                  on_conflict=OnConflict.REPLACE)
    self.add_task(task=Database().remove_safety_copies, key=("safety_copies_cleanup",), scopes=set(), executor="common",
                  quiet=True, on_conflict=OnConflict.REPLACE)
    self.request_worker_tmp_cleanup()
    self.request_backup_retention()

  def request_metrics_flush(self):
    """Queues the write of the metrics' completed minutes, the manager's and each worker's."""
    self.add_task(task=Metrics().flush, key=("flush_metrics",), scopes={metrics_scope("flush")}, executor="metrics", quiet=True)

  def request_release_check(self, quiet: bool = True) -> str:
    """Queues fetching the newest release: quietly from the schedule and at startup, visibly when asked for. The
    task's id."""
    return self.add_task(task=Releases().refresh, key=("refresh_latest_release",), scopes={"release"}, executor="common",
                         on_conflict=OnConflict.REPLACE, quiet=quiet).task_id

  def request_platform_backup(self, slot: datetime | None = None) -> str | None:
    """Queues a platform backup into the configured store, with its backup recorded queued; None without a store. The
    schedule's names its `slot` and is queued only while that slot is due (`slot_due`), taking the slot's own
    interrupted or cancelled row again. A new row is dropped when the queue does not take it. It holds only its part
    of the store, so it runs alongside other work; a request while one runs gets one of its own after it, and one that
    replaces a queued one settles that one cancelled. The task's id."""
    session = configured_session()
    if session is None:
      return None
    now, reused = utcnow(), None
    if slot is not None:
      slot, buffer = slot.replace(second=0, microsecond=0), timedelta(seconds=config.BACKUP_SLOT_BUFFER_SECONDS)
      is_due, reused = slot_due([SlotRow(row.id, row.status, row.deleted_at is not None, row.slot_at, row.updated_at, row.automatic)
                                 for row in Backup.select().where(
          (Backup.type == "platform") & (Backup.storage_id == session.identity)
          & Backup.slot_at.between(slot - buffer, slot + buffer))], slot, self.started_at, buffer)
      if not is_due:
        return None
    backup_ids = [reused] if reused else add_backups(session.identity, slot or now, slot is not None, [
        session.get_full_path(platform_backup_key(archive_name(now), config.VERSION, len(migrations.STEPS)))])
    added = self.add_task(task=settling(self.backup_platform), key=("backup_platform",),
                          scopes={archive_write_scope(session.identity, "platform")}, executor="platform",
                          params={"backup_ids": backup_ids, "storage_id": session.identity}, on_conflict=OnConflict.REPLACE)
    if added.outcome == Outcome.ACCEPTED:
      cancel_queued_backups(backup_ids)
      if reused:
        requeue_backups([reused])
    elif not reused:
      apply_catalog(CatalogPlan(deletes=backup_ids))
    return added.task_id

  def request_backup_catch_up(self, platform_cron: str):
    """Requests, once after the Manager starts, each backup schedule's most recent slot before the start that no
    backup holds: each volume's on each container of its application, and the platform's (`platform_cron`)."""
    self.request_automatic_backups(utcnow(), since_start=True)
    self.request_platform_backup(croniter(platform_cron, self.started_at).get_prev(datetime))

  async def backup_platform(self, backup_ids: list[str], storage_id: str):
    """Writes the platform's configuration (`Database.snapshot`), encrypted, into the store it was requested for, at
    its backup's key, and settles that backup succeeded. Refused once the configured store is another."""
    session = configured_session(storage_id)
    if session is None:
      logger.error("The configured store is not the one this backup was asked for; nothing was written.")
      raise TaskFailed("the backup store is not the one it was requested for")
    backup = Backup.get_or_none(Backup.id.in_(backup_ids))
    if backup is None:
      logger.info("The platform backup's record is gone; nothing was written.")
      return
    name = backup.s3_path.rsplit("/", 1)[-1].removesuffix(PLATFORM_BACKUP_SUFFIX)
    try:
      with TemporaryDirectory(dir=config.TMP_DIR) as directory:
        plain, encrypted = Path(directory) / f"{name}.db", Path(directory) / f"{name}{PLATFORM_BACKUP_SUFFIX}"
        await run_in_executor_with_context(Database().snapshot, plain)
        await run_in_executor_with_context(encrypt_file, plain, encrypted)
        key = await session.upload_to_key(encrypted, PLATFORM_PREFIX)
    except Exception as error:
      Notifications().notify(f"The platform backup failed: {error}", "error", "/backups")
      raise TaskFailed(str(error) or type(error).__name__)
    settle_backups([backup.id], "succeeded")
    logger.info(f"Platform backup written to {key}.")
    Notifications().notify("Platform backup created.", "success", "/backups")

  def request_platform_restore(self, backup_id: str) -> str:
    """Requests the restore of a completed platform backup in the configured store: from now on every other request is refused,
    and the restore starts once the tasks queued before it have finished. The task's id."""
    session = S3().session()
    backup = Backup.get_or_none((Backup.id == backup_id) & Backup.deleted_at.is_null())
    if backup is None:
      raise LookupError("Backup not found.")
    if backup.type != "platform":
      raise ValueError("Only a platform backup restores the platform.")
    if backup.status != "succeeded":
      raise Conflict("Only a completed backup can be restored.")
    if backup.storage_id != session.identity:
      raise Conflict("This backup is not in the configured store.")
    if refusal := platform_restore_refusal(backup.s3_path, len(migrations.STEPS)):
      raise Conflict(refusal)
    added = self.add_restore(task=self.restore_platform, name="restore_platform", key=("restore_platform", backup.s3_path),
                             executor="platform", params={"s3_path": backup.s3_path, "storage_id": backup.storage_id})
    if added.outcome == Outcome.REFUSED:
      raise Conflict("Sage is not running, so it can't restore now.")
    return added.task_id

  async def restore_platform(self, s3_path: str, storage_id: str):
    """Restores a platform backup as a restart of the Manager would find it. It runs alone in the queue's restoring
    state: every other request is refused, the tasks queued before it have finished, and the API refuses changes and
    the ingest API the workers' reports. It downloads the backup from the store it was requested for and installs it
    (`Database.install`: the configuration replaced, the history and the Backup rows started empty), keeps the
    configured S3 settings, installs the other settings' consumers, and clears what is held in memory of the old
    configuration; then the restoring state ends and what runs at startup is requested. A failure before the install
    leaves everything as it was."""
    restored = False
    try:
      session = configured_session(storage_id)
      if session is None:
        raise Exception("the configured store is not the one this restore was asked for")
      store = Settings().get("s3")
      with TemporaryDirectory(dir=config.TMP_DIR) as directory:
        encrypted, plain = Path(directory) / "platform.db.enc", Path(directory) / "platform.db"
        await session.download_key(s3_path, str(encrypted), raw_path=True)
        await run_in_executor_with_context(decrypt_file, encrypted, plain)
        await run_in_executor_with_context(Database().install, plain, {"s3": store})
      restored = True
      Settings().load_all()
      for consumer, key in ((S3(), "s3"), (Notifications(), "notifications")):
        saved = Settings().snapshot(key)
        consumer.install(saved.values, saved.revision)
      for owner in (Containers(), Workers(), Domains()):
        owner.clear()
      for held in (self.setup_failures, self.stack_repairs, self.removal_failures, self.recoveries, self.orphans):
        held.clear()
    except Exception as error:
      Notifications().notify(f"The platform restore from the backup taken at {taken_at(s3_path)} failed"
                             f"{'' if restored else '; nothing changed'}: {error}", "error", "/backups")
      raise TaskFailed()
    finally:
      self.end_restore()
      if restored:
        self.request_startup()
    Notifications().notify(f"Platform restored from the backup taken at {taken_at(s3_path)}.", "success", "/backups")
