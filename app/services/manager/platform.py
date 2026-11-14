"""The platform's backup, which keeps its configuration in the store, and its restore, which installs one as a restart
of the Manager would find it; what runs when Sage starts, at startup and after a restore; its daily cleanup and the
minutely write of the metrics; the check for a newer release; and the restore and backup facts the pages show."""
import logging
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from services.backups.commands import add_backup
from services.backups.keys import (BACKUP_TIMESTAMP_FORMAT, PLATFORM_BACKUP_SUFFIX, PLATFORM_PREFIX, archive_name, archive_write_scope, taken_at,
                                   timestamp_from_key)
from services.containers.targets import Containers
from services.db.database import Database
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

from .backups import configured_session

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
    """Requests the daily cleanup, each part a task of its own: the metrics, logs and history past their retention,
    and the store's old backups."""
    self.add_task(task=Metrics().cleanup, key=("metrics_cleanup",), scopes={metrics_scope()}, executor="metrics", quiet=True,
                  on_conflict=OnConflict.REPLACE)
    self.add_task(task=Logs().cleanup, key=("logs_cleanup",), scopes=set(), executor="common", quiet=True,
                  on_conflict=OnConflict.REPLACE)
    self.add_task(task=purge_history, key=("purge_history",), scopes=set(), executor="common", quiet=True,
                  on_conflict=OnConflict.REPLACE)
    self.request_backup_retention()

  def request_metrics_flush(self):
    """Queues the write of the metrics' completed minutes, the manager's and each worker's."""
    self.add_task(task=Metrics().flush, key=("flush_metrics",), scopes={metrics_scope("flush")}, executor="metrics", quiet=True)

  def request_release_check(self, quiet: bool = True) -> str:
    """Queues fetching the newest release: quietly from the schedule and at startup, visibly when asked for. The
    task's id."""
    return self.add_task(task=Releases().refresh, key=("refresh_latest_release",), scopes={"release"}, executor="common",
                         on_conflict=OnConflict.REPLACE, quiet=quiet).task_id

  def request_platform_backup(self) -> str | None:
    """Queues a platform backup into the configured store; None without one. It holds only its part of the store, so
    it runs alongside other work; a request while one runs gets one of its own after it. The task's id."""
    session = configured_session()
    if session is None:
      return None
    return self.add_task(task=self.backup_platform, key=("backup_platform",),
                         scopes={archive_write_scope(session.identity, "platform")}, executor="platform",
                         params={"storage_id": session.identity}, on_conflict=OnConflict.REPLACE).task_id

  async def backup_platform(self, storage_id: str):
    """Writes the platform's configuration (`Database.snapshot`), encrypted, into the store it was requested for, and
    records the key it uploaded. Refused once the configured store is another."""
    session = configured_session(storage_id)
    if session is None:
      logger.error("The configured store is not the one this backup was asked for; nothing was written.")
      raise TaskFailed()
    name = archive_name(utcnow())
    try:
      with TemporaryDirectory() as directory:
        plain, encrypted = Path(directory) / f"{name}.db", Path(directory) / f"{name}{PLATFORM_BACKUP_SUFFIX}"
        await run_in_executor_with_context(Database().snapshot, plain)
        await run_in_executor_with_context(encrypt_file, plain, encrypted)
        key = await session.upload_to_key(encrypted, PLATFORM_PREFIX)
    except Exception as error:
      Notifications().notify(f"The platform backup failed: {error}", "error", "/backups")
      raise TaskFailed()
    add_backup(key, session.identity)
    logger.info(f"Platform backup written to {key}.")
    Notifications().notify("Platform backup created.", "success", "/backups")

  def request_platform_restore(self, backup_id: str) -> str:
    """Requests the restore of a platform backup in the configured store: from now on every other request is refused,
    and the restore starts once the tasks queued before it have finished. The task's id."""
    session = S3().session()
    backup = Backup.get_or_none(Backup.id == backup_id)
    if backup is None:
      raise LookupError("Backup not found.")
    if backup.type != "platform":
      raise ValueError("Only a platform backup restores the platform.")
    if backup.storage_id != session.identity:
      raise Conflict("This backup is not in the configured store.")
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
      with TemporaryDirectory() as directory:
        encrypted, plain = Path(directory) / "platform.db.enc", Path(directory) / "platform.db"
        await session.download_key(s3_path, str(encrypted), raw_path=True)
        await run_in_executor_with_context(decrypt_file, encrypted, plain)
        await run_in_executor_with_context(Database().install, plain)
      restored = True
      Settings().load_all()
      Settings().set("s3", store)
      for consumer, key in ((S3(), "s3"), (Notifications(), "notifications")):
        saved = Settings().snapshot(key)
        consumer.install(saved.values, saved.revision)
      for owner in (Containers(), Workers(), Domains()):
        owner.clear()
      for held in (self.setup_failures, self.stack_repairs, self.recoveries, self.orphans):
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
