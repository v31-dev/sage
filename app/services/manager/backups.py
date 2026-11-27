"""Volume backups: the manual backup of a stopped container's volumes and the automatic one that keeps its
container's runtime, and the restore of a backup into a stopped container's volume, with the requests that queue them.
Tasks name volumes by their names in their labels: their ids mean nothing to people, and a backup can be restored into
another volume, on another container and worker."""
import logging
from dataclasses import replace
from datetime import datetime, timedelta

from croniter import croniter

import config
from services.backups.catalog import CatalogPlan
from services.backups.commands import add_backups, apply_catalog, requeue_backups, settle_backups
from services.backups.decisions import SlotRow, slot_due
from services.backups.keys import VOLUME_ARCHIVE_SUFFIX, archive_name, archive_scope, archive_write_scope, taken_at, volume_archive_key
from services.containers.decisions import maintenance_refusal
from services.containers.placement import placement_dir, placement_scope, placement_unit
from services.containers.record import Baseline, Settled
from services.containers.targets import Containers
from services.db.models import Application, Backup, Container, Project, Volume
from services.history import record_container_event
from services.notification import Notifications
from services.s3 import S3, S3Session
from services.tailscale import CommandError, Tailscale, still_running
from utils.common import get_env, new_id, utcnow
from utils.errors import Conflict
from utils.logging import TaskFailed
from utils.queue import Outcome
from utils.retry import is_transient, retry_async

from .lifecycle import REFUSALS, Refused, mark

logger = logging.getLogger(__name__)


def configured_session(storage_id: str | None = None) -> S3Session | None:
  """The configured store's session; None when none is configured, or when it is not the store `storage_id` names."""
  try:
    session = S3().session()
  except Exception:
    return None
  return session if storage_id in (None, session.identity) else None


def settling(task):
  """The backup task body `task`, its backups (`backup_ids`) set running as it starts and those it leaves unfinished
  failed when it fails, its error as their reason."""
  async def run(**arguments):
    settle_backups(arguments["backup_ids"], "running")
    # Only an outcome settles them: a body cancelled by the loop's shutdown leaves them to the next start's discovery.
    try:
      await task(**arguments)
    except Exception as error:
      settle_backups(arguments["backup_ids"], "failed", str(error) or "it could not be completed")
      raise
  return run


class BackupsMixin:
  def request_backup(self, application_id: int, volume_ids: list[str]) -> list[str]:
    """Queues a backup of the volumes from each of the application's containers into the configured store, each in
    its container's lifecycle slot with its backups recorded queued, one per volume (dropped again when the queue
    does not take it); refused unless every one of them is stopped. The ids of the tasks queued."""
    session = S3().session()
    volumes = list(Volume.select().where((Volume.application == application_id) & Volume.id.in_(volume_ids))
                   .order_by(Volume.name))
    if not volume_ids or len(volumes) != len(set(volume_ids)):
      raise ValueError("Choose volumes of this application to back up.")
    containers = self.application_containers(application_id)
    refusal = self._request_refusal("backup_stopped", containers)
    if refusal is not None:
      raise Conflict(f"A container of this application can't be backed up: {REFUSALS[refusal]}.")
    now, task_ids = utcnow(), []
    for container in containers:
      backup_ids = add_backups(session.identity, now, False, [
          session.get_full_path(volume_archive_key(volume.id, container.id, archive_name(now))) for volume in volumes],
          volumes, container)
      added = self.queue_lifecycle(container, "backup_stopped", settling(self.backup_container), {},
                                   {"backup_ids": backup_ids, "storage_id": session.identity},
                                   (archive_write_scope(session.identity, container.id),),
                                   {"Volumes": ", ".join(volume.name for volume in volumes)})
      if added.outcome == Outcome.ACCEPTED:
        task_ids.append(added.task_id)
      else:
        apply_catalog(CatalogPlan(deletes=backup_ids))
    return task_ids

  def request_automatic_backups(self, now: datetime, since_start: bool = False):
    """Queues, for each container of an application, an automatic backup of its volumes whose schedule's slot is due
    (`slot_due`) into the configured store: the slot of `now`'s minute, or with `since_start` each schedule's most
    recent slot before the Manager started; nothing without a store or while the queue is not running. Its backups are
    recorded queued for the slot, a slot's own interrupted or cancelled row taken again; the new ones are dropped when
    the queue does not take it. It has its own key, by container and volumes, on the container's scope: a user request
    waits behind a running one and is never refused or replaced by it, and the backup of other volumes due meanwhile is
    queued after it."""
    if self.task_state() != "running":
      return
    session = configured_session()
    if session is None:
      return
    minute, buffer, schedules = now.replace(second=0, microsecond=0), timedelta(seconds=config.BACKUP_SLOT_BUFFER_SECONDS), {}
    for volume in Volume.select().where(Volume.backup_cron.is_null(False) & (Volume.backup_cron != "")).order_by(Volume.name):
      if not croniter.is_valid(volume.backup_cron):
        logger.error(f"Volume {volume.name} has an invalid backup schedule: {volume.backup_cron!r}")
        continue
      if since_start:
        slot = croniter(volume.backup_cron, self.started_at).get_prev(datetime)
      elif croniter.match(volume.backup_cron, now):
        slot = minute
      else:
        continue
      schedules.setdefault((volume.application_id, slot), []).append(volume)
    for (application_id, slot), volumes in schedules.items():
      for container in (Container.select(Container, Application, Project).join(Application).join(Project)
                        .where(Container.application == application_id)):
        due = {}
        for volume in volumes:
          is_due, reused = slot_due([SlotRow(row.id, row.status, row.deleted_at is not None, row.slot_at, row.updated_at, row.automatic)
                                     for row in Backup.select().where(
              (Backup.container_id == container.id) & (Backup.source_volume_id == volume.id)
              & (Backup.storage_id == session.identity) & Backup.slot_at.between(slot - buffer, slot + buffer))],
              slot, self.started_at, buffer)
          if is_due:
            due[volume] = reused
        if not due:
          continue
        fresh = [volume for volume, reused in due.items() if reused is None]
        added_ids = iter(add_backups(session.identity, slot, True, [
            session.get_full_path(volume_archive_key(volume.id, container.id, archive_name(now))) for volume in fresh],
            fresh, container) if fresh else ())
        backup_ids = [reused or next(added_ids) for reused in due.values()]
        added = self.add_task(
            task=settling(self.backup_container_preserving),
            key=("backup_preserve", container.id, *sorted(volume.id for volume in due)),
            scopes={placement_scope(container.worker_id, container.id), archive_write_scope(session.identity, container.id)},
            executor="main", params={"container_id": container.id, "backup_ids": backup_ids, "storage_id": session.identity},
            labels={**self.container_labels(container), "Volumes": ", ".join(volume.name for volume in due)})
        if added.outcome == Outcome.ACCEPTED:
          requeue_backups([reused for reused in due.values() if reused])
        else:
          apply_catalog(CatalogPlan(deletes=[backup_id for backup_id, reused in zip(backup_ids, due.values()) if not reused]))

  def request_restore(self, container_id: str, backup_id: str, volume_id: str) -> str:
    """Queues the restore of a backup of one of the application's volumes into the container's copy of `volume_id`,
    any volume of that application, in the container's lifecycle slot; refused unless the container is stopped or never
    deployed, and the backup is completed and in the configured store. The task's id."""
    session = S3().session()
    container = self.lifecycle_container(container_id)
    backup = Backup.get_or_none((Backup.id == backup_id) & Backup.deleted_at.is_null())
    if backup is None:
      raise LookupError("Backup not found.")
    if backup.status != "succeeded":
      raise Conflict("Only a completed backup can be restored.")
    volume = Volume.get_or_none((Volume.id == volume_id) & (Volume.application == container.application_id))
    if backup.type != "application" or backup.application_id != container.application_id or volume is None:
      raise ValueError("Choose a backup of this application's volumes, and one of its volumes.")
    if backup.storage_id != session.identity:
      raise Conflict("This backup is not in the configured store.")
    refusal = self._request_refusal("restore_stopped", [container])
    if refusal is not None:
      raise Conflict(f"This container can't receive a restore: {REFUSALS[refusal]}.")
    return self.admitted_task_id(self.queue_lifecycle(
        container, "restore_stopped", self.restore_container, {},
        {"backup_id": backup.id, "volume_id": volume.id, "storage_id": session.identity},
        (archive_scope(session.identity, backup.s3_path),), {"Volume": volume.name, "Backup of": backup.source_volume_name}))

  async def backup_container(self, container_id: str, backup_ids: list[str], storage_id: str):
    """Takes the backups of a stopped container's volumes into the store it was requested for, then commits it stopped
    and succeeded. Refused while it runs, after a failed action, or once the configured store is another; an archive
    that fails fails it.
    Its backups are cancelled when the container is gone."""
    session = configured_session(storage_id)
    backups = list(Backup.select().where(Backup.id.in_(backup_ids)))
    volume_ids = [backup.source_volume_id for backup in backups]
    begun = await self.begin_lifecycle(container_id, "backup_stopped", prepare=self._backup_check("backup_stopped", session, volume_ids))
    if begun is None:
      settle_backups(backup_ids, "cancelled", "its container was deleted")
      return
    container, record = begun
    operation = record.operation
    try:
      await Containers().write(container, record)
      failures = await self._archive(container, session, backups)
      if failures:
        raise Exception("; ".join(failures))
      verified_at = await self.stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now, verified_on_worker_at=verified_at)))
    except Exception as error:
      await self.fail_lifecycle(container, record, error)
    record_container_event(container, "backup_stopped", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Backed up")
    name, page = self.container_notice(container)
    volumes = ", ".join(volume.name for volume in Volume.select(Volume.name).where(
        (Volume.application == container.application_id) & Volume.id.in_(volume_ids)).order_by(Volume.name))
    Notifications().notify(f"Backup of {volumes} from {name} created.", "success", page)

  async def backup_container_preserving(self, container_id: str, backup_ids: list[str], storage_id: str):
    """Archives the container's volumes into the store it was requested for and keeps its runtime: whether it ran,
    and then the deployment and Docker containers it ran, are recorded before it is stopped as it is, and it is
    brought back to that after the archives, whatever they did. A failed archive with the runtime back fails the task
    and keeps the container's last outcome; a runtime that can't be brought back is stopped and settled failed.
    Refused, failing the task, when its last action failed, its runtime disagrees with it, or the configured store is
    another; tried again after `BACKUP_RETRY_DELAYS` while its worker can't be reached. Its backups are cancelled
    when the container is gone."""
    session = configured_session(storage_id)
    backups = list(Backup.select().where(Backup.id.in_(backup_ids)))
    begun = await retry_async(
        lambda: self.begin_lifecycle(container_id, "backup_preserve", prepare=self._backup_check(
            "backup_preserve", session, [backup.source_volume_id for backup in backups])),
        delays=config.BACKUP_RETRY_DELAYS,
        on_retry=lambda error, delay: logger.warning(f"Automatic backup refused: {error}. Retrying in {delay}s."))
    if begun is None:
      settle_backups(backup_ids, "cancelled", "its container was deleted")
      return
    container, record = begun
    operation, host = record.operation, container.worker_id
    try:
      await Containers().write(container, record)
      if operation.baseline.runtime == "running":
        await self.quiesce_runtime(host, container.id)
      failure = "; ".join(await self._archive(container, session, backups)) or None
    except Exception as error:
      if still_running(error):
        await self.fail_lifecycle(container, record, error)
      failure = str(error) or type(error).__name__
    try:
      verified_at = await self.resume_runtime(host, container.id, operation.baseline)
    except Exception as error:
      await self.fail_lifecycle(container, record, CommandError(
          f"it could not be started again as it was before the backup: {error}", running=still_running(error)))
    try:
      await Containers().write(container, replace(record, operation=replace(
          operation, phase="settled", outcome="failed" if failure else "succeeded", reason=failure, finished_at=utcnow(),
          verified_on_worker_at=verified_at)))
    except Exception as error:
      failure = failure or f"its end could not be recorded on the worker: {error}"
    if failure is not None:
      record_container_event(container, "backup_preserve", "failed", operation.id, reason={"error": failure},
                             dedupe_key=f"{operation.id}:failed")
      await mark(container, f"Automatic backup failed: {failure}")
      name, page = self.container_notice(container)
      Notifications().notify(f"Automatic backup of {name} failed: {failure}", "error", page)
      raise TaskFailed(failure)
    record_container_event(container, "backup_preserve", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Backed up")

  async def restore_container(self, container_id: str, backup_id: str, volume_id: str, storage_id: str):
    """Replaces the container's copy of the volume with the backup, read from the store it was requested for, and
    commits the container stopped and succeeded. Refused while it runs or after a failed action, once the configured
    store is another, or when the selected backup or the volume is gone; a failed restore leaves it stopped and in
    error, the volume possibly part replaced, and a Deploy or Stop allowed."""
    session = configured_session(storage_id)
    backup = Backup.get_or_none((Backup.id == backup_id) & Backup.deleted_at.is_null() & (Backup.status == "succeeded"))

    async def prepare(container: Container, kept) -> None:
      if session is None or (backup is not None and backup.storage_id != session.identity):
        raise Refused("store_changed")
      if backup is None or backup.application_id != container.application_id or backup.source_volume_id is None:
        raise Refused("no_backup")
      if not Volume.select().where((Volume.id == volume_id) & (Volume.application == container.application_id)).exists():
        raise Refused("no_volumes")
      reading, _ = await self.inspect_runtime(container.worker_id, container.id)
      refusal = maintenance_refusal("restore_stopped", kept.settled if kept is not None else None, None, reading)
      if refusal is not None:
        raise Refused(refusal)

    begun = await self.begin_lifecycle(container_id, "restore_stopped", prepare=prepare)
    if begun is None:
      return
    container, record = begun
    operation = record.operation
    try:
      await Containers().write(container, record)
      volume = Volume.get_by_id(volume_id)
      run = f"{config.WORKER_TMP_DIR}/restore-{new_id()}"
      script = f"{run}/restore.sh"
      await Tailscale().sync_file(container.worker_id, config.TEMPLATES_DIR / "worker/application/restore.sh", script, {
          "RUN_DIR": run, "PLACEMENT_DIR": placement_dir(config.WORKER_HOME_DIR, container.id), "ARCHIVED_VOLUME_ID": backup.source_volume_id,
          "VOLUME_ID": volume.id, "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
          "DOWNLOAD_URL": session.create_presigned_download_url(backup.s3_path, config.WORKER_LINK_SECONDS, raw_path=True),
      }, mode=0o700)
      await Tailscale().exec_command(container.worker_id, script, timeout=config.BACKUP_TIMEOUT_SECONDS,
                                     unit=placement_unit(container.id))
      verified_at = await self.stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now, verified_on_worker_at=verified_at)))
    except Exception as error:
      await self.fail_lifecycle(container, record, error)
    record_container_event(container, "restore_stopped", "succeeded", operation.id,
                           reason={"volume": volume.name, "backup_of": backup.source_volume_name}, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, f"Volume {volume.name} restored from a backup of {backup.source_volume_name}")
    name, page = self.container_notice(container)
    Notifications().notify(f"Volume {volume.name} of {name} restored from the backup of {backup.source_volume_name} taken at "
                           f"{taken_at(backup.s3_path)}.", "success", page)

  def _request_refusal(self, contract: str, containers: list[Container]) -> str | None:
    """The first refusal one of the containers gives: `not_stopped` unless it is inactive (its record this Sage's, its
    runtime current and stopped), then what `maintenance_refusal` gives from that record and runtime."""
    states = self.container_states(containers)
    for container in containers:
      state = states[container.id]
      if state.view.headline != "inactive":
        return "not_stopped"
      record = state.target.record
      refusal = maintenance_refusal(contract, record.settled if record else None, None, state.runtime.reading)
      if refusal is not None:
        return refusal
    return None

  def _backup_check(self, contract: str, session: S3Session | None, volume_ids: list[str]):
    """The pre-check of a backup, for `_begin`: the store it was requested for is the configured one, one of its
    volumes is configured, and the container is as `maintenance_refusal` needs it, its runtime read now; an automatic
    backup's baseline is what it found."""
    async def prepare(container: Container, kept) -> Baseline | None:
      if session is None:
        raise Refused("store_changed")
      if not Volume.select().where((Volume.application == container.application_id) & Volume.id.in_(volume_ids)).exists():
        raise Refused("no_volumes")
      settled, applied = (kept.settled, kept.applied) if kept is not None else (None, None)
      reading, _ = await self.inspect_runtime(container.worker_id, container.id)
      refusal = maintenance_refusal(contract, settled, applied, reading)
      if refusal is not None:
        raise Refused(refusal)
      if contract != "backup_preserve":
        return None
      if settled.runtime_goal == "stopped":
        return Baseline(settled.operation_id, "stopped", None, ())
      return Baseline(settled.operation_id, "running", applied.deployment_id,
                      await self.runtime_ids(container.worker_id, container.id))
    return prepare

  async def _archive(self, container: Container, session: S3Session, backups: list[Backup]) -> list[str]:
    """Takes each of the backups from the container's placement into the store at its key, and settles it: succeeded
    once uploaded, failed with its error, cancelled when its volume is no longer configured. Those that failed
    transiently are taken again together after each of `BACKUP_RETRY_DELAYS`. What failed, one line each; a backup
    whose command may still run is not taken again, and this raises once the backups left are settled failed."""
    failures, last, waiting = [], {}, {backup.source_volume_id: backup for backup in backups}
    pending = [(volume, waiting.pop(volume.id)) for volume in Volume.select().where(
        (Volume.application == container.application_id) & Volume.id.in_(list(waiting))).order_by(Volume.name)]
    if waiting:
      settle_backups([backup.id for backup in waiting.values()], "cancelled", "its volume was deleted")

    async def attempt():
      transient = None
      for volume, backup in list(pending):
        key, name = backup.s3_path, backup.s3_path.rsplit("/", 1)[-1].removesuffix(VOLUME_ARCHIVE_SUFFIX)
        run = f"{config.WORKER_TMP_DIR}/backup-{new_id()}"
        script = f"{run}/backup.sh"
        try:
          await Tailscale().sync_file(container.worker_id, config.TEMPLATES_DIR / "worker/application/backup.sh", script, {
              "RUN_DIR": run, "PLACEMENT_DIR": placement_dir(config.WORKER_HOME_DIR, container.id), "VOLUME_ID": volume.id,
              "ARCHIVE_NAME": name, "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
              "UPLOAD_URL": session.create_presigned_upload_url(key, config.WORKER_LINK_SECONDS, raw_path=True),
          }, mode=0o700)
          await Tailscale().exec_command(container.worker_id, script, timeout=config.BACKUP_TIMEOUT_SECONDS,
                                         unit=placement_unit(container.id))
        except Exception as error:
          if still_running(error):
            raise
          last[backup.id] = error
          if is_transient(error):
            transient = transient or error
            continue
          settle_backups([backup.id], "failed", str(error) or type(error).__name__)
          failures.append(f"volume {volume.name}: {error}")
        else:
          settle_backups([backup.id], "succeeded")
          logger.info(f"Volume {volume.name} of container {container.id} backed up to {key}.")
        pending.remove((volume, backup))
      if transient is not None:
        raise transient

    try:
      await retry_async(attempt, delays=config.BACKUP_RETRY_DELAYS, on_retry=lambda error, delay: logger.warning(
          f"Volumes of container {container.id} could not be backed up: {error}. Retrying in {delay}s."))
    except Exception as error:
      for volume, backup in pending:
        reason = last.get(backup.id, error)
        settle_backups([backup.id], "failed", str(reason) or type(reason).__name__)
        failures.append(f"volume {volume.name}: {reason}")
      if still_running(error):
        raise
    return failures
