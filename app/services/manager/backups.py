"""Volume backups: the manual backup of a stopped container's volumes and the automatic one that keeps its
container's runtime, with the requests that queue them."""
import logging
from dataclasses import replace
from datetime import datetime

from croniter import croniter

import config
from services.backups.keys import BACKUP_TIMESTAMP_FORMAT, volume_archive_key
from services.containers.decisions import maintenance_refusal
from services.containers.placement import placement_dir
from services.containers.record import Authority, Baseline, Settled
from services.containers.targets import Containers
from services.db.models import Application, Backup, Container, Instance, Project, Volume
from services.history import record_container_event
from services.s3 import S3, S3Session
from services.tailscale import Tailscale
from services.updates import Updates, UpdateSource
from utils.common import get_env, new_id, utcnow
from utils.errors import Conflict
from utils.logging import TaskFailed
from utils.queue import Outcome

from .containers import REFUSALS, Refused, mark

logger = logging.getLogger(__name__)


def configured_session(storage_id: str | None = None) -> S3Session | None:
  """The configured store's session; None when none is configured, or when it is not the store `storage_id` names."""
  try:
    session = S3().session()
  except Exception:
    return None
  return session if storage_id in (None, session.identity) else None


class BackupsMixin:
  def request_backup(self, application_id: int, volume_ids: list[str]) -> list[str]:
    """Queues a backup of the volumes from each of the application's containers into the configured store, each in
    its container's lifecycle slot; refused unless every one of them is stopped. The ids of the tasks queued."""
    session = S3().session()
    if not volume_ids or Volume.select().where(
            (Volume.application == application_id) & Volume.id.in_(volume_ids)).count() != len(set(volume_ids)):
      raise ValueError("Choose volumes of this application to back up.")
    containers = self._application_containers(application_id)
    instance, states = Instance.get(), self.container_states(containers)
    for container in containers:
      state = states[container.id]
      record = state.target.record
      kept = record if record is not None and record.authority == Authority(
          instance.id, instance.state_generation, container.id) else None
      refusal = maintenance_refusal("backup_stopped", kept.settled if kept else None, None,
                                    state.runtime.reading if state.runtime.knowledge == "known" else None)
      if refusal is not None:
        raise Conflict(f"A container of this application can't be backed up: {REFUSALS[refusal]}.")
    added = [self._request_lifecycle(container, "backup_stopped", self.backup_container, {},
                                     {"volume_ids": sorted(set(volume_ids)), "storage_id": session.identity},
                                     (f"backup:store:{session.identity}:write:{container.id}",))
             for container in containers]
    return [result.task_id for result in added if result.outcome == Outcome.ACCEPTED]

  def request_automatic_backups(self, now: datetime):
    """Queues, for each container of an application with volumes due now by their schedule and not backed up this
    minute, an automatic backup of those volumes into the configured store; nothing without one. It has its own key
    on the container's scope: a user request waits behind a running one and is never refused or replaced by it."""
    session = configured_session()
    if session is None:
      return
    minute, due = now.replace(second=0, microsecond=0), {}
    for volume in Volume.select().where(Volume.backup_cron.is_null(False) & (Volume.backup_cron != "")):
      if not croniter.is_valid(volume.backup_cron):
        logger.error(f"Volume {volume.name} has an invalid backup schedule: {volume.backup_cron!r}")
        continue
      if croniter.match(volume.backup_cron, now) and not Backup.select().where(
              (Backup.source_volume_id == volume.id) & (Backup.created_at >= minute)).exists():
        due.setdefault(volume.application_id, []).append(volume.id)
    for container in (Container.select(Container, Application, Project).join(Application).join(Project)
                      .where(Container.application.in_(list(due)))):
      self.add_task(task=self.backup_container_preserving, key=("backup_preserve", container.id),
                    scopes={f"worker:{container.worker_id}:container:{container.id}",
                            f"backup:store:{session.identity}:write:{container.id}"},
                    executor="app", params={"container_id": container.id, "volume_ids": sorted(due[container.application_id]),
                                            "storage_id": session.identity},
                    labels=self._labels(container))

  async def backup_container(self, container_id: str, volume_ids: list[str], storage_id: str):
    """Archives the volumes of a stopped container into the store it was requested for, then commits it stopped and
    succeeded. Refused while it runs, or once the configured store is another; an archive that fails fails it."""
    session = configured_session(storage_id)
    begun = await self._begin(container_id, "backup_stopped", prepare=self._backup_check("backup_stopped", session, volume_ids))
    if begun is None:
      return
    container, record = begun
    operation = record.operation
    try:
      await Containers().write(container, record)
      failures = await self._archive(container, session, volume_ids)
      if failures:
        raise Exception("; ".join(failures))
      verified_at = await self._stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now, verified_on_worker_at=verified_at)))
    except Exception as error:
      await self._fail(container, record, error)
    record_container_event(container, "backup_stopped", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Backed up")
    # TODO(36 step 4H): self.notify(f"Backup created for container {container.id} on worker {container.worker_id}.", "success")

  async def backup_container_preserving(self, container_id: str, volume_ids: list[str], storage_id: str):
    """Archives the container's volumes into the store it was requested for and keeps its runtime: whether it ran,
    and then the deployment and Docker containers it ran, are recorded before it is stopped as it is, and it is
    brought back to that after the archives, whatever they did. A failed archive with the runtime back fails the task
    and keeps the container's last outcome; a runtime that can't be brought back is stopped and settled failed.
    Refused, failing the task, when its last action failed, its runtime disagrees with it, or the configured store is
    another."""
    session = configured_session(storage_id)
    begun = await self._begin(container_id, "backup_preserve", prepare=self._backup_check("backup_preserve", session, volume_ids))
    if begun is None:
      return
    container, record = begun
    operation, host = record.operation, container.worker_id
    try:
      await Containers().write(container, record)
      if operation.baseline.runtime == "running":
        await self._quiesce(host, container.id)
      failure = "; ".join(await self._archive(container, session, volume_ids)) or None
    except Exception as error:
      failure = str(error) or type(error).__name__
    try:
      verified_at = await self._resume(host, container.id, operation.baseline)
    except Exception as error:
      await self._fail(container, record, Exception(f"it could not be started again as it was before the backup: {error}"))
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
      logger.error(f"Automatic backup failed: {failure}")
      # TODO(36 step 4H): self.notify(f"Automatic backup of container {container.id} failed: {failure}", "error")
      raise TaskFailed()
    record_container_event(container, "backup_preserve", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Backed up")

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
      reading, _ = await self._inspect(container.worker_id, container.id)
      refusal = maintenance_refusal(contract, settled, applied, reading)
      if refusal is not None:
        raise Refused(refusal)
      if contract != "backup_preserve":
        return None
      if settled.runtime_goal == "stopped":
        return Baseline(settled.operation_id, "stopped", None, ())
      return Baseline(settled.operation_id, "running", applied.deployment_id,
                      await self._runtime_ids(container.worker_id, container.id))
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
      Backup.create(s3_path=key, storage_id=session.identity, type="application", source_volume_id=volume.id,
                    source_volume_name=volume.name, application=container.application_id)
      Updates().changed(UpdateSource.BACKUPS)
      logger.info(f"Volume {volume.name} of container {container.id} backed up to {key}.")
    return failures
