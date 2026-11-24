"""The configured store's backup catalog: a backup's archive deleted before its row is marked deleted, the Backup table
made to mirror the store, and the backups and rows past the retention period removed, with the requests that queue them. Each holds the store's
scope, or a part of it, by its identity, and does nothing to another store: one configured since is never touched."""
import logging
from datetime import timedelta
from functools import partial

import config
from services.applications.commands import delete_application, delete_volume
from services.backups.catalog import UNFINISHED, CatalogPlan, CatalogRow, reconcile_catalog
from services.backups.commands import apply_catalog, mark_deleted
from services.backups.keys import APPLICATIONS_PREFIX, PLATFORM_PREFIX, archive_scope, is_expired_backup_key, store_scope, taken_at
from services.db.models import Backup, Container, Volume
from services.notification import Notifications
from utils.common import utcnow
from utils.errors import Conflict
from utils.logging import TaskFailed
from utils.queue import OnConflict

from .backups import configured_session

logger = logging.getLogger(__name__)


class BackupCatalogMixin:
  def request_backup_deletion(self, backup_id: str) -> str | None:
    """Queues deletion of a completed backup's archive, then marks its rows in that store deleted, the task's id; a
    backup that was not completed has no archive and is marked deleted at once, with None. Refused while it is being
    taken."""
    backup = Backup.get_or_none((Backup.id == backup_id) & Backup.deleted_at.is_null())
    if backup is None:
      raise LookupError("Backup not found.")
    if backup.status in UNFINISHED:
      raise Conflict("A backup that is still being taken can't be deleted.")
    if backup.status != "succeeded":
      mark_deleted([backup.id])
      return None
    return self.add_task(task=self.delete_archive, key=("delete_archive", backup.storage_id, backup.s3_path),
                         scopes={archive_scope(backup.storage_id, backup.s3_path)}, executor="common",
                         params={"s3_path": backup.s3_path, "storage_id": backup.storage_id},
                         labels={"Backup of": backup.source_volume_name if backup.type == "application" else "the platform"}).task_id

  async def delete_archive(self, s3_path: str, storage_id: str):
    """Deletes the archive and then marks its rows in that store deleted; refused when the store is no longer
    configured."""
    session = configured_session(storage_id)
    if session is None:
      logger.error(f"The archive {s3_path} is in a store that is no longer the configured one; it was left there.")
      raise TaskFailed()
    try:
      await session.delete_key(s3_path, filter=lambda key: key == s3_path, raw_path=True)
    except Exception as error:
      Notifications().notify(f"The backup taken at {taken_at(s3_path)} could not be deleted from S3: {error}", "error", "/backups")
      raise TaskFailed()
    mark_deleted(list(Backup.select(Backup.id).where((Backup.storage_id == storage_id) & (Backup.s3_path == s3_path)).scalars()))
    logger.info(f"Deleted the archive {s3_path}.")

  def request_volume_deletion(self, volume_id: str):
    """Deletes the volume (`delete_volume`), then queues the deletion of its archives in the configured store; refused
    unless every container of its application is inactive with nothing queued on it, since a queued Deploy mounts the
    volumes it was requested with."""
    volume = Volume.get_or_none(Volume.id == volume_id)
    if volume is None:
      raise LookupError("Volume not found.")
    for container in Container.select().where(Container.application == volume.application_id):
      state = self.container_states([container])[container.id]
      if state.view.headline != "inactive" or state.queued:
        raise Conflict("A volume can be deleted only while every container of its application is stopped.")
    delete_volume(volume_id)
    self.request_volume_archives_deletion([volume])

  def request_application_deletion(self, application_id: int):
    """Deletes the application (`delete_application`), then queues the deletion of each of its volumes' archives in
    the configured store; their rows went with it."""
    volumes = list(Volume.select().where(Volume.application == application_id))
    delete_application(application_id)
    self.request_volume_archives_deletion(volumes)

  def request_volume_archives_deletion(self, volumes: list[Volume]):
    """Queues, for each deleted volume, the deletion of its archives in the configured store, holding the store, so
    it waits for a backup still writing there; nothing without a store, the archives left where they are."""
    session = configured_session()
    if session is None:
      if volumes:
        logger.info("No backup store is configured, so the archives of the deleted volumes are left where they are.")
      return
    for volume in volumes:
      self.add_task(task=self.delete_volume_archives, key=("delete_volume_archives", session.identity, volume.id),
                    scopes={store_scope(session.identity)}, executor="common",
                    params={"volume_id": volume.id, "storage_id": session.identity}, labels={"Volume": volume.name})

  async def delete_volume_archives(self, volume_id: str, storage_id: str):
    """Deletes every archive of the deleted volume from the store, with one listing of its folder, then marks its rows
    in that store deleted. Nothing when the configured store is another by now."""
    session = configured_session(storage_id)
    if session is None:
      logger.info("The configured store is not the one this deletion was asked for; nothing to do.")
      return
    try:
      deleted = await session.delete_key(f"{APPLICATIONS_PREFIX}/{volume_id}")
    except Exception as error:
      Notifications().notify(f"The backups of a deleted volume could not be removed from S3: {error}", "error", "/backups")
      raise TaskFailed()
    mark_deleted(list(Backup.select(Backup.id).where(
        (Backup.source_volume_id == volume_id) & (Backup.storage_id == storage_id)).scalars()))
    logger.info(f"Removed the {len(deleted)} archive(s) of deleted volume {volume_id}.")

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
    store (`reconcile_catalog`): rows of another store, and completed ones of an object gone, are dropped; rows left
    unfinished by an earlier run of the Manager are settled; listed backups no row records are added, a volume archive
    only while its volume is configured. Nothing when the configured store is another by now, whose own
    discovery follows its save."""
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
    plan = reconcile_catalog(
        session.identity, session.target.path, platform_keys, volume_keys,
        [CatalogRow(row.id, row.s3_path, row.storage_id, row.status, row.deleted_at is not None, row.updated_at)
         for row in Backup.select(Backup.id, Backup.s3_path, Backup.storage_id, Backup.status, Backup.deleted_at,
                                  Backup.updated_at)],
        {volume.id: (volume.application_id, volume.name) for volume in Volume.select(Volume.id, Volume.application, Volume.name)},
        self.started_at, {container.id: container.worker_id for container in Container.select(Container.id, Container.worker)})
    apply_catalog(plan)
    logger.info(f"Backups discovered: {len(plan.inserts)} added, {len(plan.settles)} settled, {len(plan.deletes)} dropped, "
                f"{plan.unmatched} archive(s) of no configured volume left out.")

  def request_backup_retention(self):
    """Queues the removal of the configured store's backups past the retention period; nothing without a store."""
    session = configured_session()
    if session is None:
      return
    self.add_task(task=self.prune_backups, key=("prune_backups",), scopes={store_scope(session.identity)},
                  executor="common", params={"storage_id": session.identity}, quiet=True)

  async def prune_backups(self, storage_id: str):
    """Deletes from the store the platform backups and volume archives older than `config.RETENTION_DAYS`, then the rows
    of those it deleted, those of a prefix that failed included, and the store's rows of that age that are not a
    completed backup or were deleted by the user. Nothing when the configured store is another by now."""
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
    apply_catalog(CatalogPlan(deletes=list(Backup.select(Backup.id).where((Backup.storage_id == session.identity) & (
        Backup.s3_path.in_(deleted) | ((Backup.created_at < cutoff)
                                       & ((Backup.status != "succeeded") | Backup.deleted_at.is_null(False))))).scalars())))
    if failure is not None:
      Notifications().notify(f"Old backups could not all be removed from S3: {failure}", "error", "/backups")
      raise TaskFailed()
    logger.info(f"Removed {len(deleted)} backup(s) older than {config.RETENTION_DAYS} days from the store.")
