import asyncio
import logging
from datetime import datetime
from pathlib import Path

from croniter import croniter

from services.db import APPLICATION_BUSY_STATUSES, Application, Backup, Container, Event, Volume
from utils.common import get_env
from utils.logging import generate_task_id_token, task_id

from ._common import BACKUP_TIMESTAMP_FORMAT, app_dir

logger = logging.getLogger(__name__)

BACKUP_ELIGIBLE_STATUSES = {"active", "inactive"}


class BackupsMixin:
  def is_volume_backup_due(self, volume: Volume, now: datetime) -> bool:
    if not volume.backup_cron:
      return False

    if not croniter.is_valid(volume.backup_cron):
      logger.error(f"Invalid backup cron for application {volume.application.qualified_name} volume {volume.name}: {volume.backup_cron!r}")
      return False

    if not croniter.match(volume.backup_cron, now):
      return False

    # Due this cron minute: de-dupe by skipping if a backup for this volume was
    # already made since the start of the current minute.
    window_start = now.replace(second=0, microsecond=0)
    return not Backup.select().where(
        (Backup.type == "application")
        & (Backup.application == volume.application)
        & (Backup.source_volume_name == volume.name)
        & (Backup.created_at >= window_start)
    ).exists()

  def get_volume_backup_resource_error(
      self,
      application: Application,
      volumes: list[Volume],
  ) -> str | None:
    if application.container_count == 0:
      return f"Application {application.qualified_name} has no containers to back up."

    if not volumes:
      return f"Application {application.qualified_name} has no volumes to back up."

    if any(volume.application_id != application.id for volume in volumes):
      return f"One or more selected volumes do not belong to application {application.qualified_name}."

    return None

  def get_due_volume_backups(self, now: datetime):
    volumes = Volume.select(Volume, Application).join(Application).where(
        (Volume.backup_cron.is_null(False))
        & (Volume.backup_cron != "")
        & (Application.status.in_(BACKUP_ELIGIBLE_STATUSES))
        & (~Application.status.in_(APPLICATION_BUSY_STATUSES))
        & (Application.container_count > 0)
    )

    due_backups_by_application_id = {}

    for volume in volumes:
      if not self.is_volume_backup_due(volume, now):
        continue

      if volume.application_id not in due_backups_by_application_id:
        due_backups_by_application_id[volume.application_id] = {
            "application": volume.application,
            "volumes": [],
        }

      due_backups_by_application_id[volume.application_id]["volumes"].append(volume)

    return [
        (entry["application"], entry["volumes"])
        for entry in due_backups_by_application_id.values()
    ]

  def _get_application_backup_snapshot(self, application: Application):
    return {
        "application_status": application.status,
        "containers": [
            {
                "id": container.id,
                "status": container.status,
                "worker": container.worker.hostname,
            }
            for container in application.containers
        ],
    }

  def _restore_application_backup_snapshot(self, application: Application, snapshot: dict):
    application.status = snapshot["application_status"]
    application.save()

    for container_state in snapshot["containers"]:
      container = Container.get_by_id(container_state["id"])
      container.status = container_state["status"]
      container.save()

  def _set_application_backup_status(self, application: Application):
    application.status = "backup"
    application.save()

    for container in application.containers:
      if container.status != "backup":
        container.status = "backup"
        container.save()

  def _set_application_restore_status(self, application: Application, target_container: Container):
    application.status = "restoring"
    application.save()

    if target_container.status != "restoring":
      target_container.status = "restoring"
      target_container.save()

  def _build_application_backup_prefix(self, application: Application) -> str:
    return (
        f"{self.s3_backup_path_applications}/"
        f"{application.project.name}/{application.name}"
    )

  def _build_application_volume_backup_key(
      self,
      application: Application,
      container: Container,
      volume: Volume,
      timestamp: str,
  ) -> str:
    return (
        f"{self._build_application_backup_prefix(application)}/"
        f"{container.worker.hostname}/{volume.name}/{timestamp}.tar.gz.enc"
    )

  def _get_application_backup_unit_label(
      self,
      application: Application,
      container: Container,
      volume: Volume,
  ) -> str:
    return (
        f"{application.qualified_name} "
        f"on worker {container.worker.hostname} volume {volume.name}"
    )

  async def _stop_container_for_backup(self, container: Container):
    if container.status != "active":
      return

    container_dir = f"{self.worker_home_dir}/applications/{container.application.qualified_name}"
    logger.info(
        f"Stopping application {container.application.qualified_name} container on worker {container.worker.hostname} for backup."
    )
    await self.tailscale.exec_command(
        container.worker.hostname,
        f"docker compose -f {container_dir}/docker-compose.yml down",
    )

  async def _start_container_after_backup(self, container: Container):
    container_dir = f"{self.worker_home_dir}/applications/{container.application.qualified_name}"
    logger.info(
        f"Starting application {container.application.qualified_name} container on worker {container.worker.hostname} after backup."
    )

    try:
      await self.tailscale.exec_command(
          container.worker.hostname,
          f"docker compose -f {container_dir}/docker-compose.yml up -d --wait",
      )
      container.status = "active"
      container.save()
    except Exception:
      container.status = "error"
      container.save()
      raise

  async def _restore_application_runtime_after_backup(self, application: Application, snapshot: dict):
    if snapshot["application_status"] != "active":
      self._restore_application_backup_snapshot(application, snapshot)
      return

    containers = [Container.get_by_id(entry["id"]) for entry in snapshot["containers"]]
    await asyncio.gather(
        *[self._start_container_after_backup(container) for container in containers],
        return_exceptions=False,
    )

    application.status = "active"
    application.save()

  async def _backup_application_container_volume_to_s3(
      self,
      application: Application,
      container: Container,
      volume: Volume,
      timestamp: str,
  ):
    container_task_id = generate_task_id_token()
    object_key = self._build_application_volume_backup_key(
        application,
        container,
        volume,
        timestamp,
    )
    archive_name = Path(object_key).name.removesuffix(".tar.gz.enc")
    upload_url = self.s3.create_presigned_upload_url(object_key)
    backup_unit_label = self._get_application_backup_unit_label(application, container, volume)
    remote_script_path = f"/tmp/sage-backup-{container_task_id}.sh"

    Event.create(
        container=container,
        type="backup",
        application_task_id=task_id.get(),
        container_task_id=container_task_id,
    )

    logger.info(f"Starting backup for {backup_unit_label} with task id {container_task_id}.")

    task_id_token = task_id.set(container_task_id)
    try:
      await self.tailscale.sync_file(
          container.worker.hostname,
          app_dir / "templates/worker/application/backup.sh",
          remote_script_path,
          {
              "WORKER_HOME_DIR": self.worker_home_dir,
              "APP_NAME": application.qualified_name,
              "VOLUME_NAME": volume.name,
              "ARCHIVE_NAME": archive_name,
              "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
              "UPLOAD_URL": upload_url,
          })
      await self.tailscale.exec_command(
          container.worker.hostname,
          f"chmod 700 {remote_script_path} && {remote_script_path}",
          900,
      )
      s3_path = self.s3.get_full_path(object_key)
      Backup.create(
          s3_path=s3_path,
          type="application",
          source_volume_name=volume.name,
          application=application,
      )
      self.notify(f"Backup created for {backup_unit_label} at {s3_path}.", "success")
    except Exception as exc:
      self.notify(f"Failed to create backup for {backup_unit_label}: {exc}", "error")
      raise
    finally:
      task_id.reset(task_id_token)

  async def backup_application_s3(self, application_id: int, volume_ids: list[int] | None = None):
    application = Application.get_or_none(Application.id == application_id)
    if application is None:
      logger.info(f"Application {application_id} deleted before backup; nothing to do.")
      return

    if application.status not in BACKUP_ELIGIBLE_STATUSES:
      raise Exception(
          f"Application {application.qualified_name} must be active or inactive before backup."
      )

    snapshot = self._get_application_backup_snapshot(application)
    timestamp = datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
    if volume_ids is None:
      volumes = list(application.volumes)
    else:
      volumes = list(
          Volume.select().where(
              (Volume.application == application)
              & (Volume.id.in_(volume_ids))
          )
      )

    resource_error = self.get_volume_backup_resource_error(application, volumes)
    if resource_error:
      raise Exception(resource_error)

    # Fail before stopping any container: the backup needs every worker, and an
    # offline one would otherwise wedge the app mid-backup until the timeout.
    offline_workers = sorted({
        container.worker.hostname
        for container in application.containers
        if not container.worker.online
    })
    if offline_workers:
      raise Exception(
          f"Cannot back up {application.qualified_name}: worker(s) offline: {', '.join(offline_workers)}."
      )

    failed_backup_units = []

    try:
      self._set_application_backup_status(application)

      if snapshot["application_status"] == "active":
        await asyncio.gather(
            *[
                self._stop_container_for_backup(Container.get_by_id(entry["id"]))
                for entry in snapshot["containers"]
            ],
            return_exceptions=False,
        )

      for entry in snapshot["containers"]:
        container = Container.get_by_id(entry["id"])
        for volume in volumes:
          try:
            await self._backup_application_container_volume_to_s3(
                application,
                container,
                volume,
                timestamp,
            )
          except Exception as exc:
            failed_backup_units.append(
                f"{self._get_application_backup_unit_label(application, container, volume)}: {exc}"
            )

      if failed_backup_units:
        raise Exception(
            f"Failed backup units for {application.qualified_name}: {'; '.join(failed_backup_units)}"
        )

      await self._restore_application_runtime_after_backup(application, snapshot)
    except Exception:
      try:
        await self._restore_application_runtime_after_backup(application, snapshot)
      except Exception as restore_exc:
        logger.error(
            f"Failed to restore application {application.qualified_name} to its previous state after backup failure: {restore_exc}")
        application.status = "error"
        application.save()
        for entry in snapshot["containers"]:
          container = Container.get_by_id(entry["id"])
          if container.status in ["backup", "restoring"]:
            container.status = "error"
            container.save()
      raise

  async def delete_backup_s3(self, s3_path: str):
    try:
      await self.s3.delete_key(s3_path, raw_path=True)
      logger.info(f"Successfully deleted S3 backup: {s3_path}")
    except Exception as e:
      logger.error(f"Failed to delete S3 backup {s3_path}: {e}")
      raise

  async def restore_application_volume_from_s3(
      self,
      application_id: int,
      volume_id: int,
      backup_id: int,
      target_worker_hostname: str,
  ):
    application = Application.get_by_id(application_id)
    volume = Volume.get_by_id(volume_id)
    backup = Backup.get_by_id(backup_id)

    if application.status != "inactive":
      raise ValueError(
          f"Application {application.qualified_name} must be inactive before restore."
      )

    resource_error = self.get_volume_backup_resource_error(application, [volume])
    if resource_error:
      raise ValueError(resource_error)

    if backup.type != "application":
      raise ValueError("Only application volume backups can be restored.")

    if backup.application_id != application.id or backup.source_volume_name != volume.name:
      raise ValueError("Selected backup does not belong to the requested volume.")

    target_container = Container.get_or_none(
        (Container.application == application)
        & (Container.worker == target_worker_hostname)
    )
    if not target_container:
      raise ValueError(
          f"Target worker {target_worker_hostname} is not attached to application {application.qualified_name}."
      )

    if not target_container.worker.online:
      raise ValueError(f"Target worker {target_worker_hostname} is offline.")

    restore_task_id = task_id.get()
    task_id_token = None
    if not restore_task_id:
      restore_task_id = generate_task_id_token()
      task_id_token = task_id.set(restore_task_id)

    snapshot = self._get_application_backup_snapshot(application)
    restore_unit_label = self._get_application_backup_unit_label(application, target_container, volume)
    remote_script_path = f"/tmp/sage-restore-{restore_task_id}.sh"
    download_url = self.s3.create_presigned_download_url(backup.s3_path, raw_path=True)

    Event.create(
        container=target_container,
        type="restore",
        application_task_id=restore_task_id,
        container_task_id=restore_task_id,
    )

    logger.info(
        f"Starting restore for {restore_unit_label} from {backup.s3_path} with task id {restore_task_id}."
    )

    try:
      self._set_application_restore_status(application, target_container)
      await self.tailscale.sync_file(
          target_container.worker.hostname,
          app_dir / "templates/worker/application/restore.sh",
          remote_script_path,
          {
              "WORKER_HOME_DIR": self.worker_home_dir,
              "APP_NAME": application.qualified_name,
              "VOLUME_NAME": volume.name,
              "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
              "DOWNLOAD_URL": download_url,
          },
      )
      await self.tailscale.exec_command(
          target_container.worker.hostname,
          f"chmod 700 {remote_script_path} && {remote_script_path}",
          900,
      )
      self.notify(
          f"Restore completed for {restore_unit_label} from {backup.s3_path}.",
          "success",
      )
    except Exception as exc:
      self.notify(
          f"Failed to restore {restore_unit_label} from {backup.s3_path}: {exc}. "
          "The volume may be partially restored; run the restore again before deploying.",
          "error",
      )
      raise Exception(f"Failed to restore {restore_unit_label}: {exc}") from exc
    finally:
      self._restore_application_backup_snapshot(application, snapshot)
      if task_id_token is not None:
        task_id.reset(task_id_token)
