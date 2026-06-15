import docker
import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from functools import partial
from tempfile import TemporaryDirectory

from services.db import (
    Backup,
    Event,
    Notification,
    Task,
    DB_PATH,
    db,
)

logger = logging.getLogger(__name__)


class PlatformMixin:
  async def discover_s3_platform_backups(self):
    """
    Discover backups in S3 and reconcile with database.

    Finds all backup files in S3 and creates Backup records in the database
    for any backups that exist in S3 but not yet in the Backup table.

    This is useful for fresh installs with existing S3 backups.
    """
    try:
      logger.info("Starting S3 backup discovery...")

      # Get all backup keys from S3
      s3_backups = await self.s3.get_keys(self.s3_backup_path_platform)

      if not s3_backups:
        logger.info("No backups found in S3")
        return

      logger.info(f"Found {len(s3_backups)} backup(s) in S3")

      # Get all existing backups from database
      existing_backups = list(backup.s3_path for backup in Backup.select().where(Backup.type == "platform"))

      # Create records for any missing backups
      new_backups = []
      for s3_path in s3_backups:
        if s3_path not in existing_backups:
          try:
            # Extract timestamp from S3 path (format: sage/backups/platform/TIMESTAMP.db)
            # TIMESTAMP format: YYYYMMDD_HHMMSS
            filename = s3_path.split('/')[-1]
            timestamp_str = filename.split('.')[0]

            # Parse timestamp to datetime (format: YYYYMMDD_HHMMSS)
            backup_datetime = datetime.strptime(timestamp_str, self.backup_timestamp_format)

            # Create backup record with extracted timestamp as created_at
            Backup.create(s3_path=s3_path, type="platform", created_at=backup_datetime)
            new_backups.append((s3_path, timestamp_str))
            logger.info(f"Created backup record for S3 backup: {s3_path}")
          except Exception as e:
            logger.error(f"Failed to create backup record for {s3_path}: {e}")

      if new_backups:
        self.notify(
            f"S3 backup discovery: recovered {len(new_backups)} existing backup(s) from S3")
      else:
        logger.info("S3 backup discovery: all backups already in database")

    except Exception as e:
      self.notify(f"S3 backup discovery failed: {e}", "error")

  async def cleanup(self, days: int = 7):
    cutoff = datetime.now() - timedelta(days=days)

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

    # Cleanup application backups from S3
    try:
      deleted_application_backups = await self.s3.delete_key(
          self.s3_backup_path_applications,
          filter=partial(self._is_expired_backup_key, cutoff=cutoff),
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
          filter=partial(self._is_expired_backup_key, cutoff=cutoff),
      )
      logger.info(
          f"Platform backup cleanup: removed {len(deleted_platform_backups)} backup files older than {days} days."
      )
    except Exception as e:
      logger.error(f"Failed to cleanup old S3 backups: {e}")

  def is_platform_backup_in_progress(self) -> bool:
    """Check if a platform backup is currently in progress."""
    return self.platform_backup_in_progress

  async def backup_database_s3(self):
    # Check if backup already in progress
    if self.platform_backup_in_progress:
      logger.warning("Platform backup already in progress, skipping")
      return

    try:
      self.platform_backup_in_progress = True

      # Create the backup
      with TemporaryDirectory() as temp_dir:
        timestamp = datetime.now().strftime(self.backup_timestamp_format)
        backup_file = f"{temp_dir}/{timestamp}.db"

        try:
          # Use SQLite's native backup API
          src_conn = db.connection()
          backup_conn = sqlite3.connect(backup_file)
          src_conn.backup(backup_conn)
          backup_conn.close()

          # Upload to S3
          await self.s3.upload_to_key(backup_file, self.s3_backup_path_platform)

          # Record backup in database
          full_backup_path = f"{self.s3.get_full_path(self.s3_backup_path_platform)}/{timestamp}.db"
          Backup.create(s3_path=full_backup_path, type="platform")

          self.notify(f"Database backup created at {timestamp}.")
        except Exception as e:
          self.notify(f"Failed to create database backup: {e}", "error")
          raise Exception(f"Failed to create database backup: {e}")
    finally:
      self.platform_backup_in_progress = False

  def restart(self, all: bool = False, sage: bool = False, traefik: bool = False, vector: bool = False, glances: bool = False):
    client = docker.from_env()

    if all or glances:
      client.containers.get("glances").restart()

    if all or vector:
      client.containers.get("vector").restart()

    if all or traefik:
      client.containers.get("traefik").restart()

    if all or sage:
      client.containers.get("sage").restart()

  async def restore_database_from_s3(self, s3_path: str):
    """
    Restore database from S3 backup.

    Process:
    - Download backup file from S3 to temporary location
    - Validate it's a valid SQLite database
    - Create safety backup of current database
    - Restore into the live database using SQLite's online backup API
    - Verify database integrity
    - Re-sync workers
    """
    try:
      logger.info(f"Starting database restore from {s3_path}")

      with TemporaryDirectory() as temp_dir:
        # Download backup from S3
        downloaded_backup = f"{temp_dir}/restore_backup.db"
        logger.info(f"Downloading backup from S3: {s3_path}")
        await self.s3.download_key(s3_path, downloaded_backup, raw_path=True)

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
        timestamp = datetime.now().strftime(self.backup_timestamp_format)
        safety_backup_path = f"{DB_PATH}.backup.{timestamp}"
        logger.info(f"Creating safety backup: {safety_backup_path}")

        try:
          shutil.copy2(DB_PATH, safety_backup_path)
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
        except Exception as e:
          logger.error(f"Database integrity verification failed: {e}")
          # Rollback to safety backup
          logger.warning(f"Integrity check failed, rolling back from safety backup: {safety_backup_path}")
          try:
            rollback_conn = sqlite3.connect(safety_backup_path)
            live_conn = db.connection()
            rollback_conn.backup(live_conn)
            rollback_conn.close()
            self.notify(
                "Platform restore completed with fallback to safety backup",
                "warning"
            )
            return
          except Exception as rollback_err:
            logger.error(f"Failed to rollback to safety backup: {rollback_err}")
          raise Exception(f"Database integrity check failed and recovery unsuccessful: {e}")

        self.notify(f"Platform restored from backup {s3_path} successfully.", "success")

        # The restore rewrote worker state in the DB; queue a force-resync.
        # cancel_existing supersedes a pending sync; queue=True lets it wait
        # behind this restore, which holds the same scope.
        self.add_task(
            task=self.sync_workers,
            scopes={"platform", "app"},
            params={"force": True},
            executor="platform",
            cancel_existing=True,
            queue=True,
        )

    except Exception as e:
      self.notify(f"Platform restore failed: {e}", "error")
      raise
