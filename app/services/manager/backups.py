"""Volume backups: the manual backup of a stopped container's volumes and the automatic one that keeps its
container's runtime, and the restore of a backup into a stopped container's volume, with the requests that queue them.
Tasks name volumes by their names in their labels: their ids mean nothing to people, and a backup can be restored into
another volume, on another container and worker."""
import hashlib
import logging
from dataclasses import replace
from datetime import datetime

from croniter import croniter

import config
from services.backups.commands import add_backup
from services.backups.keys import BACKUP_TIMESTAMP_FORMAT, taken_at, volume_archive_key
from services.containers.decisions import maintenance_refusal
from services.containers.placement import placement_dir
from services.containers.record import Authority, Baseline, Settled
from services.containers.targets import Containers
from services.db.models import Application, Backup, Container, Instance, Project, Volume
from services.history import record_container_event
from services.notification import Notifications
from services.s3 import S3, S3Session
from services.tailscale import Tailscale
from utils.common import get_env, new_id, utcnow
from utils.errors import Conflict
from utils.logging import TaskFailed
from utils.queue import Outcome

from .lifecycle import REFUSALS, Refused, mark

logger = logging.getLogger(__name__)


def configured_session(storage_id: str | None = None) -> S3Session | None:
  """The configured store's session; None when none is configured, or when it is not the store `storage_id` names."""
  try:
    session = S3().session()
  except Exception:
    return None
  return session if storage_id in (None, session.identity) else None


def archive_scope(storage_id: str, s3_path: str) -> str:
  """The scope of one archive in its store, which a restore from it and its deletion share."""
  return f"backup:store:{storage_id}:archive:{hashlib.sha256(s3_path.encode()).hexdigest()[:16]}"


class BackupsMixin:
  def request_backup(self, application_id: int, volume_ids: list[str]) -> list[str]:
    """Queues a backup of the volumes from each of the application's containers into the configured store, each in
    its container's lifecycle slot; refused unless every one of them is stopped. The ids of the tasks queued."""
    session = S3().session()
    names = [volume.name for volume in Volume.select(Volume.name).where(
        (Volume.application == application_id) & Volume.id.in_(volume_ids)).order_by(Volume.name)]
    if not volume_ids or len(names) != len(set(volume_ids)):
      raise ValueError("Choose volumes of this application to back up.")
    containers = self.application_containers(application_id)
    refusal = self._request_refusal("backup_stopped", containers)
    if refusal is not None:
      raise Conflict(f"A container of this application can't be backed up: {REFUSALS[refusal]}.")
    added = [self.queue_lifecycle(container, "backup_stopped", self.backup_container, {},
                                  {"volume_ids": sorted(set(volume_ids)), "storage_id": session.identity},
                                  (f"backup:store:{session.identity}:write:{container.id}",), {"Volumes": ", ".join(names)})
             for container in containers]
    return [result.task_id for result in added if result.outcome == Outcome.ACCEPTED]

  def request_automatic_backups(self, now: datetime):
    """Queues, for each container of an application with volumes due now by their schedule and not backed up this
    minute, an automatic backup of those volumes into the configured store; nothing without one. It has its own key
    on the container's scope: a user request waits behind a running one and is never refused or replaced by it."""
    session = configured_session()
    if session is None:
      return
    minute, due, names = now.replace(second=0, microsecond=0), {}, {}
    for volume in Volume.select().where(Volume.backup_cron.is_null(False) & (Volume.backup_cron != "")):
      if not croniter.is_valid(volume.backup_cron):
        logger.error(f"Volume {volume.name} has an invalid backup schedule: {volume.backup_cron!r}")
        continue
      if croniter.match(volume.backup_cron, now) and not Backup.select().where(
              (Backup.source_volume_id == volume.id) & (Backup.created_at >= minute)).exists():
        due.setdefault(volume.application_id, []).append(volume.id)
        names.setdefault(volume.application_id, []).append(volume.name)
    for container in (Container.select(Container, Application, Project).join(Application).join(Project)
                      .where(Container.application.in_(list(due)))):
      self.add_task(task=self.backup_container_preserving, key=("backup_preserve", container.id),
                    scopes={f"worker:{container.worker_id}:container:{container.id}",
                            f"backup:store:{session.identity}:write:{container.id}"},
                    executor="app", params={"container_id": container.id, "volume_ids": sorted(due[container.application_id]),
                                            "storage_id": session.identity},
                    labels={**self.container_labels(container), "Volumes": ", ".join(sorted(names[container.application_id]))})

  def request_restore(self, container_id: str, backup_id: int, volume_id: str) -> str:
    """Queues the restore of a backup of one of the application's volumes into the container's copy of `volume_id`,
    any volume of that application, in the container's lifecycle slot; refused unless the container is stopped or never
    deployed, and the backup is in the configured store. The task's id."""
    session = S3().session()
    container = self.lifecycle_container(container_id)
    backup = Backup.get_or_none(Backup.id == backup_id)
    if backup is None:
      raise LookupError("Backup not found.")
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

  async def backup_container(self, container_id: str, volume_ids: list[str], storage_id: str):
    """Archives the volumes of a stopped container into the store it was requested for, then commits it stopped and
    succeeded. Refused while it runs, or once the configured store is another; an archive that fails fails it."""
    session = configured_session(storage_id)
    begun = await self.begin_lifecycle(container_id, "backup_stopped", prepare=self._backup_check("backup_stopped", session, volume_ids))
    if begun is None:
      return
    container, record = begun
    operation = record.operation
    try:
      await Containers().write(container, record)
      failures = await self._archive(container, session, volume_ids)
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

  async def backup_container_preserving(self, container_id: str, volume_ids: list[str], storage_id: str):
    """Archives the container's volumes into the store it was requested for and keeps its runtime: whether it ran,
    and then the deployment and Docker containers it ran, are recorded before it is stopped as it is, and it is
    brought back to that after the archives, whatever they did. A failed archive with the runtime back fails the task
    and keeps the container's last outcome; a runtime that can't be brought back is stopped and settled failed.
    Refused, failing the task, when its last action failed, its runtime disagrees with it, or the configured store is
    another."""
    session = configured_session(storage_id)
    begun = await self.begin_lifecycle(container_id, "backup_preserve", prepare=self._backup_check("backup_preserve", session, volume_ids))
    if begun is None:
      return
    container, record = begun
    operation, host = record.operation, container.worker_id
    try:
      await Containers().write(container, record)
      if operation.baseline.runtime == "running":
        await self.quiesce_runtime(host, container.id)
      failure = "; ".join(await self._archive(container, session, volume_ids)) or None
    except Exception as error:
      failure = str(error) or type(error).__name__
    try:
      verified_at = await self.resume_runtime(host, container.id, operation.baseline)
    except Exception as error:
      await self.fail_lifecycle(container, record, Exception(f"it could not be started again as it was before the backup: {error}"))
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
      raise TaskFailed()
    record_container_event(container, "backup_preserve", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Backed up")

  async def restore_container(self, container_id: str, backup_id: int, volume_id: str, storage_id: str):
    """Replaces the container's copy of the volume with the backup, read from the store it was requested for, and
    commits the container stopped and succeeded. Refused while it runs, once the configured store is another, or when
    the backup or the volume is gone; a failed restore leaves it stopped and in error, the volume possibly part
    replaced, and a Deploy allowed."""
    session = configured_session(storage_id)

    async def prepare(container: Container, kept) -> None:
      backup = Backup.get_or_none((Backup.id == backup_id) & (Backup.application == container.application_id))
      if session is None or (backup is not None and backup.storage_id != session.identity):
        raise Refused("store_changed")
      if backup is None or backup.source_volume_id is None:
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
      backup, volume = Backup.get_by_id(backup_id), Volume.get_by_id(volume_id)
      script = f"/tmp/sage-restore-{new_id()}.sh"
      await Tailscale().sync_file(container.worker_id, config.TEMPLATES_DIR / "worker/application/restore.sh", script, {
          "PLACEMENT_DIR": placement_dir(config.WORKER_HOME_DIR, container.id), "ARCHIVED_VOLUME_ID": backup.source_volume_id,
          "VOLUME_ID": volume.id, "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
          "DOWNLOAD_URL": session.create_presigned_download_url(backup.s3_path, config.WORKER_LINK_SECONDS, raw_path=True),
      }, mode=0o700)
      await Tailscale().exec_command(container.worker_id, script, timeout=config.BACKUP_TIMEOUT_SECONDS)
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
    """The first refusal `maintenance_refusal` gives one of the containers, from its record and its runtime as held."""
    instance, states = Instance.get(), self.container_states(containers)
    for container in containers:
      state = states[container.id]
      record = state.target.record
      kept = record if record is not None and record.authority == Authority(
          instance.id, instance.state_generation, container.id) else None
      refusal = maintenance_refusal(contract, kept.settled if kept else None, None,
                                    state.runtime.reading if state.runtime.knowledge == "known" else None)
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

  async def _archive(self, container: Container, session: S3Session, volume_ids: list[str]) -> list[str]:
    """Archives each of the volumes configured among `volume_ids` from the container's placement into the store, each
    with its Backup row once uploaded; what failed, one line each."""
    timestamp, failures = utcnow().strftime(BACKUP_TIMESTAMP_FORMAT), []
    for volume in (Volume.select().where((Volume.application == container.application_id) & Volume.id.in_(volume_ids))
                   .order_by(Volume.name)):
      key = session.get_full_path(volume_archive_key(volume.id, container.id, timestamp))
      script = f"/tmp/sage-backup-{new_id()}.sh"
      try:
        await Tailscale().sync_file(container.worker_id, config.TEMPLATES_DIR / "worker/application/backup.sh", script, {
            "PLACEMENT_DIR": placement_dir(config.WORKER_HOME_DIR, container.id), "VOLUME_ID": volume.id,
            "ARCHIVE_NAME": timestamp, "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
            "UPLOAD_URL": session.create_presigned_upload_url(key, config.WORKER_LINK_SECONDS, raw_path=True),
        }, mode=0o700)
        await Tailscale().exec_command(container.worker_id, script, timeout=config.BACKUP_TIMEOUT_SECONDS)
      except Exception as error:
        failures.append(f"volume {volume.name}: {error}")
        continue
      add_backup(key, session.identity, volume.id, volume.name, container.application_id)
      logger.info(f"Volume {volume.name} of container {container.id} backed up to {key}.")
    return failures
