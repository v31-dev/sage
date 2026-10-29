import logging
import sqlite3
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import InvalidToken
from peewee import chunked

import config
from services.db import Application, Backup, Database, Event, Notification, Task, Worker, db
from services.notification import Notifications
from services.settings import Settings
from services.updates import Fact, Updates, UpdateSource
from utils.archive import BackupDecryptError, decrypt_file, encrypt_file
from utils.common import utcnow
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict

from ._common import BACKUP_TIMESTAMP_FORMAT, is_expired_backup_key, timestamp_from_key
from .application import application_changed

logger = logging.getLogger(__name__)


class PlatformMixin:
  def platform_queue_facts(self) -> list[Fact]:
    return [Fact(self.restoring_from, UpdateSource.RESTORING), Fact(self.backup_status, UpdateSource.BACKUP_STATUS)]

  def backup_status(self) -> dict:
    """Whether a platform backup is running and whether backups are being discovered."""
    return {
        "platform_backup_in_progress": self.is_task_running(self.backup_database_s3),
        "discovery_in_progress": self.has_task(self.discover_s3_backups),
    }

  async def discover_s3_backups(self):
    """
    Make the Backup table match what's in S3 under the configured path: record the
    platform backups and volume archives found there, and drop records whose object is
    gone. A fresh manager pointed at an existing bucket then lists its backups.

    A volume archive is recorded only while its application exists here, matched by
    the project and application names in its key.
    """
    try:
      platform_keys = set(await self.s3.get_keys(self.s3_backup_path_platform))
      volume_keys = set(await self.s3.get_keys(self.s3_backup_path_applications))
    except Exception as e:
      self.notify(f"Backup discovery failed: {e}", "error")
      return

    volume_prefix = self.s3.get_full_path(self.s3_backup_path_applications) + "/"

    def reconcile():
      recorded = {backup.s3_path: backup.id for backup in Backup.select(Backup.id, Backup.s3_path)}
      applications = {
          (application.project_id, application.name): application
          for application in Application.select()
      }

      rows = []
      added = {"platform": 0, "application": 0}
      unmatched = 0
      for key in sorted(platform_keys | volume_keys):
        if key in recorded:
          continue
        timestamp = timestamp_from_key(key)
        if not timestamp:
          continue
        created_at = datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT)
        if key in platform_keys:
          rows.append(dict(s3_path=key, type="platform", created_at=created_at))
          added["platform"] += 1
          continue
        # <project>/<application>/<worker>/<volume>/<timestamp>.tar.gz.enc
        parts = key.removeprefix(volume_prefix).split("/")
        if len(parts) != 5 or not key.endswith(".tar.gz.enc"):
          continue
        application = applications.get((parts[0], parts[1]))
        if not application:
          unmatched += 1
          continue
        rows.append(dict(s3_path=key, type="application", application=application,
                         source_volume_name=parts[3], created_at=created_at))
        added["application"] += 1

      present = platform_keys | volume_keys
      gone = [backup_id for s3_path, backup_id in recorded.items() if s3_path not in present]
      with db.atomic():
        for batch in chunked(rows, 100):
          Backup.insert_many(batch).execute()
        removed = Backup.delete().where(Backup.id.in_(gone)).execute() if gone else 0
      return added, removed, unmatched

    added, removed, unmatched = await run_in_executor_with_context(reconcile)

    logger.info(
        f"Backup discovery: {len(platform_keys)} platform and {len(volume_keys)} volume backup(s) in S3; "
        f"added {added['platform']} platform and {added['application']} volume record(s), "
        f"removed {removed} whose object is gone, skipped {unmatched} for applications not on this manager."
    )
    found = [f"{count} {kind} backup(s)" for kind, count in
             (("platform", added["platform"]), ("volume", added["application"])) if count]
    if found:
      self.notify(f"Backup discovery listed {' and '.join(found)} found in S3.")

  def restoring_from(self) -> str | None:
    """When the backup being restored was made (taken from its key), while restoring."""
    if self.task_state() != "restoring":
      return None
    snapshot = self.task_snapshot()
    for task in (*snapshot["running"], *snapshot["queued"]):
      if task["name"] == self.restore_database_from_s3.__name__:
        timestamp = timestamp_from_key(task["params"]["s3_path"])
        if timestamp:
          return datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT).isoformat()
    return None

  async def cleanup(self, days: int = config.RETENTION_DAYS):
    cutoff = utcnow() - timedelta(days=days)

    # Cleanup events
    deleted_count = (Event.delete().where(Event.created_at < cutoff).execute())
    logger.info(f"Events cleanup: removed {deleted_count} event rows older than {days} days.")

    # Cleanup notifications
    deleted_count = (Notification.delete().where(Notification.created_at < cutoff).execute())
    logger.info(
        f"Notifications cleanup: removed {deleted_count} notification rows older than {days} days.")

    # Cleanup backups
    deleted_count = (Backup.delete().where(Backup.created_at < cutoff).execute())
    logger.info(
        f"Backups cleanup: removed {deleted_count} backups rows older than {days} days.")

    # Cleanup task log
    deleted_count = (Task.delete().where(Task.created_at < cutoff).execute())
    logger.info(f"Tasks cleanup: removed {deleted_count} task rows older than {days} days.")

    # Cleanup local DB-restore safety copies (data.db.backup.<ts>) past retention.
    # They exist only to roll back an in-flight restore, so old ones are dead weight.
    db_file = config.DB_PATH
    removed = 0
    for path in db_file.parent.glob(f"{db_file.name}.backup.*"):
      if is_expired_backup_key(path.name, cutoff):
        path.unlink(missing_ok=True)
        removed += 1
    logger.info(f"Restore safety-copy cleanup: removed {removed} older than {days} days.")

    if not self.s3.s3_config:
      return

    # Cleanup application backups from S3
    try:
      deleted_application_backups = await self.s3.delete_key(
          self.s3_backup_path_applications,
          filter=partial(is_expired_backup_key, cutoff=cutoff),
      )
      logger.info(
          f"Application backup cleanup: removed {len(deleted_application_backups)} backup files older than {days} days."
      )
    except Exception as e:
      logger.error(f"Failed to cleanup old application backups: {e}")

    # Cleanup platform backups from S3
    try:
      deleted_platform_backups = await self.s3.delete_key(
          self.s3_backup_path_platform,
          filter=partial(is_expired_backup_key, cutoff=cutoff),
      )
      logger.info(
          f"Platform backup cleanup: removed {len(deleted_platform_backups)} backup files older than {days} days."
      )
    except Exception as e:
      logger.error(f"Failed to cleanup old S3 backups: {e}")

  async def backup_database_s3(self):
    # Create the backup
    with TemporaryDirectory() as temp_dir:
      timestamp = utcnow().strftime(BACKUP_TIMESTAMP_FORMAT)
      snapshot_file = Path(temp_dir) / f"{timestamp}.db"
      backup_file = Path(temp_dir) / f"{timestamp}.db.enc"

      try:
        # Use SQLite's native backup API
        src_conn = db.connection()
        backup_conn = sqlite3.connect(snapshot_file)
        src_conn.backup(backup_conn)
        backup_conn.close()

        # Encrypted as a whole with ENCRYPTION_KEY, in the format of the volume archives
        # (utils/archive.py): the backup reveals nothing without the key, and a restore
        # with another key fails before it touches the live database.
        await run_in_executor_with_context(encrypt_file, snapshot_file, backup_file)

        # Upload to S3
        await self.s3.upload_to_key(backup_file, self.s3_backup_path_platform)

        # Record backup in database
        full_backup_path = f"{self.s3.get_full_path(self.s3_backup_path_platform)}/{backup_file.name}"
        Backup.create(s3_path=full_backup_path, type="platform")

        self.notify(f"Database backup created at {timestamp}.")
      except Exception as e:
        self.notify(f"Failed to create database backup: {e}", "error")
        raise Exception(f"Failed to create database backup: {e}")

  async def restore_database_from_s3(self, s3_path: str):
    """
    Restore database from S3 backup.

    Process:
    - Download backup file from S3 to temporary location
    - Check and decrypt it (a backup made with another key fails here)
    - Validate it's a valid SQLite database
    - Create safety backup of current database
    - Restore into the live database using SQLite's online backup API
    - Verify database integrity
    - Keep this manager's own S3 settings (a restore never changes them)
    - Re-sync workers

    Runs while the queue is restoring (see TaskQueue): nothing else starts and the UI is
    blocked. The task ends that state, discarding the tasks queued against the old data
    when the restore took effect.
    """
    restored = False
    try:
      logger.info(f"Starting database restore from {s3_path}")
      s3_settings = Settings().get("s3")

      with TemporaryDirectory() as temp_dir:
        # Download backup from S3
        encrypted_backup = Path(temp_dir) / "restore_backup.db.enc"
        downloaded_backup = f"{temp_dir}/restore_backup.db"
        logger.info(f"Downloading backup from S3: {s3_path}")
        await self.s3.download_key(s3_path, str(encrypted_backup), raw_path=True)

        logger.info("Decrypting downloaded backup")

        def decrypt_backup():
          try:
            decrypt_file(encrypted_backup, Path(downloaded_backup))
          except BackupDecryptError as e:
            raise Exception(f"the backup {e}")

        await run_in_executor_with_context(decrypt_backup)

        # Validate it's a valid SQLite database
        logger.info("Validating downloaded backup file")
        try:
          validate_conn = sqlite3.connect(downloaded_backup)
          cursor = validate_conn.cursor()
          cursor.execute("PRAGMA integrity_check")
          result = cursor.fetchone()
          validate_conn.close()

          if result[0] != "ok":
            raise Exception(f"Downloaded backup failed integrity check: {result[0]}")
        except Exception as e:
          raise Exception(f"Invalid SQLite database in backup: {e}")

        # Create safety backup of current database
        timestamp = utcnow().strftime(BACKUP_TIMESTAMP_FORMAT)
        safety_backup_path = f"{config.DB_PATH}.backup.{timestamp}"
        logger.info(f"Creating safety backup: {safety_backup_path}")

        try:
          # Through SQLite, not a file copy: in WAL mode recent writes are only in the
          # -wal file, which a copy of the main file would miss.
          safety_conn = sqlite3.connect(safety_backup_path)
          db.connection().backup(safety_conn)
          safety_conn.close()
          logger.info(f"Safety backup created: {safety_backup_path}")
        except Exception as e:
          logger.error(f"Failed to create safety backup: {e}")
          raise Exception(f"Could not create safety backup: {e}")

        # Restore into the live database using SQLite's online backup API.
        # This writes page-by-page directly into the live DB file without closing
        # connections or touching the WAL/SHM files, avoiding disk I/O errors.
        logger.info("Restoring database using SQLite online backup API")
        try:
          restore_conn = sqlite3.connect(downloaded_backup)
          live_conn = db.connection()
          restore_conn.backup(live_conn)
          restore_conn.close()
          logger.info("Database restored successfully")
        except Exception as e:
          logger.error(f"Failed to restore database: {e}")
          # Rollback to safety backup using the same API
          logger.warning(f"Attempting rollback from safety backup: {safety_backup_path}")
          try:
            rollback_conn = sqlite3.connect(safety_backup_path)
            live_conn = db.connection()
            rollback_conn.backup(live_conn)
            rollback_conn.close()
            self.notify(
                f"Platform restore from backup {s3_path} failed, rolled back to safety backup",
                "warning"
            )
            return
          except Exception as rollback_err:
            logger.error(f"Failed to rollback to safety backup: {rollback_err}")
          raise Exception(f"Failed to restore database: {e}")

        # Verify database integrity
        logger.info("Verifying database integrity")
        try:
          verify_conn = db.connection()
          cursor = verify_conn.cursor()
          cursor.execute("PRAGMA integrity_check")
          result = cursor.fetchone()

          if result[0] != "ok":
            raise Exception(f"Restored database failed integrity check: {result[0]}")
          logger.info("Database integrity verified successfully")

          # Forward-migrate the restored snapshot to the current models: the
          # backup may predate columns/tables the running code expects, and the
          # online backup copied its older schema in wholesale.
          logger.info("Reconciling restored schema with current models")
          Database().reconcile_schema()
          logger.info("Schema reconciled successfully")

          # Reload the cached settings from the restored database (they must decrypt,
          # or the restore rolls back).
          Settings().load_all()
          Settings().set("s3", s3_settings)
        except Exception as e:
          logger.error(f"Restored database verification failed: {e}")
          # Rollback to safety backup
          logger.warning(f"Verification failed, rolling back from safety backup: {safety_backup_path}")
          try:
            rollback_conn = sqlite3.connect(safety_backup_path)
            live_conn = db.connection()
            rollback_conn.backup(live_conn)
            rollback_conn.close()
            Settings().load_all()
            reason = "it was made with a different encryption key" if isinstance(e, InvalidToken) else e
            self.notify(
                f"Platform restore from backup {s3_path} failed ({reason}); the previous database was kept.",
                "warning"
            )
            return
          except Exception as rollback_err:
            logger.error(f"Failed to rollback to safety backup: {rollback_err}")
          raise Exception(f"Database verification failed and recovery unsuccessful: {e}")

        # Services built from the settings pick up the restored values.
        self.s3.load()
        Notifications().load_notifications_config()
        self.load_workers()
        # The pages of applications and workers followed the rows that were replaced.
        for application in Application.select():
          application_changed(application)
        for worker in Worker.select():
          Updates().changed(UpdateSource.WORKER_CONTAINERS, worker.hostname)

        self.notify(f"Platform restored from backup {s3_path} successfully.", "success")
        restored = True

    except Exception as e:
      self.notify(f"Platform restore failed: {e}", "error")
      raise
    finally:
      # The restore rewrote worker state in the DB; force-resync the workers.
      follow_ups = [dict(task=self.sync_workers, scopes={"platform"}, params={"force": True},
                         executor="platform", on_conflict=OnConflict.REPLACE)]
      if self.s3.s3_config:
        # The backup records are the snapshot's too; match them to the bucket again.
        follow_ups.append(dict(task=self.discover_s3_backups, scopes={"platform", "app"}, executor="platform"))
      self.end_restore(restored, follow_ups=follow_ups)
