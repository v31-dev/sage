import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import docker

from services.db import DB_PATH, Backup, Database, Event, Notification, Task, db
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict

from ._common import BACKUP_TIMESTAMP_FORMAT, app_dir, is_expired_backup_key, timestamp_from_key

logger = logging.getLogger(__name__)

UPGRADER_IMAGE = "docker:28-cli"
UPGRADER_NAME = "sage-upgrade"
UPGRADE_SCRIPT = "templates/manager/upgrade.sh"


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
            timestamp_str = timestamp_from_key(s3_path)
            backup_datetime = datetime.strptime(timestamp_str, BACKUP_TIMESTAMP_FORMAT)

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

    # Cleanup local DB-restore safety copies (data.db.backup.<ts>) past retention.
    # They exist only to roll back an in-flight restore, so old ones are dead weight.
    db_file = Path(DB_PATH)
    removed = 0
    for path in db_file.parent.glob(f"{db_file.name}.backup.*"):
      if is_expired_backup_key(path.name, cutoff):
        path.unlink(missing_ok=True)
        removed += 1
    logger.info(f"Restore safety-copy cleanup: removed {removed} older than {days} days.")

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
      timestamp = datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
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

  def restart(self):
    client = docker.from_env()
    client.containers.get("sage").restart()

  async def upgrade(self, version: str):
    """Back up the DB, then spawn the detached compose updater and return. Aborts if
    the backup fails. The manager is replaced mid-flight, so the outcome isn't tracked
    here; updater logs survive under `docker logs sage-upgrade`."""
    await self.backup_database_s3()
    await run_in_executor_with_context(self._spawn_upgrader, version)

  def _spawn_upgrader(self, version: str):
    """Launch the detached updater container. Self-upgrade is compose-only; refuse
    (no-op + notify) when the manager wasn't started by Docker Compose."""
    client = docker.from_env()
    labels = client.containers.get("sage").labels

    working_dir = labels.get("com.docker.compose.project.working_dir")
    config_files = labels.get("com.docker.compose.project.config_files")
    project = labels.get("com.docker.compose.project")
    if not (working_dir and config_files and project):
      self.notify(
          "Upgrade aborted: this manager was not started with Docker Compose, so it "
          "cannot self-upgrade. Pull the new image tag and recreate it manually.",
          "error",
      )
      return

    # Reap a previous updater so its name/logs don't block this run.
    for stale in client.containers.list(all=True, filters={"name": UPGRADER_NAME}):
      stale.remove(force=True)

    client.containers.run(
        image=UPGRADER_IMAGE,
        command=["sh", "-c", (app_dir / UPGRADE_SCRIPT).read_text()],
        environment={
            "NEW_TAG": version,
            "PROJECT_DIR": working_dir,
            "PROJECT_NAME": project,
            "CONFIG_FILES": config_files,
        },
        volumes={
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            working_dir: {"bind": working_dir, "mode": "rw"},
        },
        working_dir=working_dir,
        name=UPGRADER_NAME,
        detach=True,
        remove=False,
    )
    self.notify(f"Upgrade to v{version} started; the manager will restart.", "info")

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
        timestamp = datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
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

          # Forward-migrate the restored snapshot to the current models: the
          # backup may predate columns/tables the running code expects, and the
          # online backup copied its older schema in wholesale.
          logger.info("Reconciling restored schema with current models")
          Database().reconcile_schema()
          logger.info("Schema reconciled successfully")
        except Exception as e:
          logger.error(f"Restored database verification failed: {e}")
          # Rollback to safety backup
          logger.warning(f"Verification failed, rolling back from safety backup: {safety_backup_path}")
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
          raise Exception(f"Database verification failed and recovery unsuccessful: {e}")

        self.notify(f"Platform restored from backup {s3_path} successfully.", "success")

        # The restore rewrote worker state in the DB; queue a force-resync.
        # REPLACE supersedes a pending sync and waits behind this restore, whose
        # scopes cover the sync's platform scope.
        self.add_task(
            task=self.sync_workers,
            scopes={"platform"},
            params={"force": True},
            executor="platform",
            on_conflict=OnConflict.REPLACE,
        )

    except Exception as e:
      self.notify(f"Platform restore failed: {e}", "error")
      raise
