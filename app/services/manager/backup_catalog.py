"""The configured store's backup catalog: a backup's archive deleted after its row, the Backup table made to mirror the
store, and the backups past the retention period removed, with the requests that queue them. Each holds the store's
scope, or a part of it, by its identity, and does nothing to another store: one configured since is never touched."""
import logging
from datetime import timedelta
from functools import partial

import config
from services.backups.catalog import CatalogPlan, CatalogRow, reconcile_catalog
from services.backups.commands import apply_catalog, remove_backup
from services.backups.keys import APPLICATIONS_PREFIX, PLATFORM_PREFIX, archive_scope, is_expired_backup_key, store_scope, taken_at
from services.db.models import Backup, Volume
from services.notification import Notifications
from utils.common import utcnow
from utils.logging import TaskFailed
from utils.queue import OnConflict

from .backups import configured_session

logger = logging.getLogger(__name__)


class BackupCatalogMixin:
  def request_backup_deletion(self, backup_id: int) -> str:
    """Deletes the backup's row now and queues the deletion of its archive from the store it is in. The task's id."""
    backup = remove_backup(backup_id)
    return self.add_task(task=self.delete_archive, key=("delete_archive", backup.storage_id, backup.s3_path),
                         scopes={archive_scope(backup.storage_id, backup.s3_path)}, executor="common",
                         params={"s3_path": backup.s3_path, "storage_id": backup.storage_id},
                         labels={"Backup of": backup.source_volume_name if backup.type == "application" else "the platform"}).task_id

  async def delete_archive(self, s3_path: str, storage_id: str):
    """Deletes the archive from the store it is in; refused when that is no longer the configured store, so an object
    of the same key in another store is never touched."""
    session = configured_session(storage_id)
    if session is None:
      logger.error(f"The archive {s3_path} is in a store that is no longer the configured one; it was left there.")
      raise TaskFailed()
    try:
      await session.delete_key(s3_path, filter=lambda key: key == s3_path, raw_path=True)
    except Exception as error:
      Notifications().notify(f"The backup taken at {taken_at(s3_path)} could not be deleted from S3: {error}", "error", "/backups")
      raise TaskFailed()
    logger.info(f"Deleted the archive {s3_path}.")

  def request_backup_discovery(self) -> str | None:
    """Queues making the Backup table mirror the configured store; None without a store. A request while one runs gets
    one of its own after it. The task's id."""
    session = configured_session()
    if session is None:
      return None
    return self.add_task(task=self.discover_backups, key=("discover_backups",), scopes={store_scope(session.identity)},
                         executor="common", params={"storage_id": session.identity}, on_conflict=OnConflict.REPLACE).task_id

  async def discover_backups(self, storage_id: str):
    """Lists the platform backups and volume archives in the store, then reads the rows, and makes the table mirror the
    store: rows of another store, and of an object gone, are dropped; listed backups not recorded are added, a volume
    archive only while its volume is configured. An archive whose deletion is queued or running is left out. Nothing
    when the configured store is another by now, whose own discovery follows its save."""
    session = configured_session(storage_id)
    if session is None:
      logger.info("The configured store is not the one this discovery was asked for; nothing to do.")
      return
    try:
      platform_keys = set(await session.get_keys(PLATFORM_PREFIX))
      volume_keys = set(await session.get_keys(APPLICATIONS_PREFIX))
    except Exception as error:
      logger.error(f"The backups in the store could not be listed: {error}")
      raise TaskFailed()
    snapshot = self.task_snapshot()
    deleting = {task["key"][2] for task in (*snapshot["running"], *snapshot["queued"])
                if task["name"] == "delete_archive" and task["key"][1] == session.identity}
    plan = reconcile_catalog(
        session.identity, session.target.path, platform_keys - deleting, volume_keys - deleting,
        [CatalogRow(row.id, row.s3_path, row.storage_id) for row in Backup.select(Backup.id, Backup.s3_path, Backup.storage_id)],
        {volume.id: (volume.application_id, volume.name) for volume in Volume.select(Volume.id, Volume.application, Volume.name)})
    apply_catalog(plan)
    logger.info(f"Backups discovered: {len(plan.inserts)} added, {len(plan.deletes)} dropped, "
                f"{plan.unmatched} archive(s) of no configured volume left out.")

  def request_backup_retention(self):
    """Queues the removal of the configured store's backups past the retention period; nothing without a store."""
    session = configured_session()
    if session is None:
      return
    self.add_task(task=self.prune_backups, key=("prune_backups",), scopes={store_scope(session.identity)},
                  executor="common", params={"storage_id": session.identity}, on_conflict=OnConflict.REPLACE, quiet=True)

  async def prune_backups(self, storage_id: str):
    """Deletes from the store the platform backups and volume archives older than `config.RETENTION_DAYS`, then the rows
    of those it deleted, those of a prefix that failed included. Nothing when the configured store is another by now."""
    session = configured_session(storage_id)
    if session is None:
      logger.info("The configured store is not the one this retention was asked for; nothing to do.")
      return
    cutoff, deleted, failure = utcnow() - timedelta(days=config.RETENTION_DAYS), [], None
    for prefix in (PLATFORM_PREFIX, APPLICATIONS_PREFIX):
      try:
        deleted += await session.delete_key(prefix, filter=partial(is_expired_backup_key, cutoff=cutoff))
      except Exception as error:
        failure = f"{failure + '; ' if failure else ''}{prefix}: {error}"
    apply_catalog(CatalogPlan(deletes=[row.id for row in Backup.select(Backup.id).where(
        (Backup.storage_id == session.identity) & Backup.s3_path.in_(deleted))]))
    if failure is not None:
      Notifications().notify(f"Old backups could not all be removed from S3: {failure}", "error", "/backups")
      raise TaskFailed()
    logger.info(f"Removed {len(deleted)} backup(s) older than {config.RETENTION_DAYS} days from the store.")
