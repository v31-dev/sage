import asyncio
import json
import logging
import re
import sqlite3
import shutil
from functools import partial
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from rocketry.time import Cron

from services.base import Base
from services.cloudflare import Cloudflare
from services.db import (
    APPLICATION_BUSY_STATUSES,
    Application,
    Container,
    Database,
    Event,
    Volume,
    Worker,
    Notification,
    Setting,
    Backup,
    DB_PATH,
    db,
)
from services.metrics import Metrics
from services.settings import Settings
from services.tailscale import Tailscale
from services.traefik import Traefik
from services.notification import Notifications
from services.s3 import S3
from utils.common import get_env
from utils.logging import generate_task_id_token, run_in_executor_with_context, task_id


app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)

BACKUP_ELIGIBLE_STATUSES = {"active", "inactive"}


class Manager(Base):
  worker_home_dir = "/opt/sage"
  s3_backup_path_platform = "/backups/platform"
  s3_backup_path_applications = "/backups/applications"
  backup_timestamp_format = "%Y%m%d_%H%M%S"
  platform_backup_in_progress = False

  def __init__(self):
    super().__init__()

    try:
      logger.info("Running Manager setup...")

      # Initialize all services
      Database()
      Settings()
      Notifications()
      Metrics()
      self.tailscale = Tailscale()
      self.traefik = Traefik(self)
      self.cloudflare = Cloudflare()
      self.s3 = S3()

      self.notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")

  async def async_init(self):
    """
    Perform async initialization after Manager.__init__().
    - Discovers and reconciles S3 backups with the database.
    """
    try:
      await self.discover_s3_platform_backups()
    except Exception as e:
      logger.error(f"S3 backup discovery failed during startup: {e}")

  async def sync_traefik_certificates(self):
    await self.traefik.sync_certificates_to_workers()

  def notify(self, message: str, type: str = "info", link: str | None = None):
    # Python logging has no success level; treat it as info.
    log_method = logger.info if type == "success" else getattr(logger, type, logger.info)
    log_method(message)
    notification = Notification.create(content=message, type=type, link=link)
    Notifications().dispatch(notification)

  def sync_workers(self):
    """
    Check for worker changes.
    - New workers (not in db but in tailscale)
    - Offline workers (in db but tailscale says offline)
    - Updated workers (IPs have changed)
    - Recently online workers (previously offline but now back online)
    - Offline workers (in db but not in tailscale)
    """
    existing_workers = list(Worker.select())
    tailscale_workers = self.tailscale.get_by_tag(
        get_env("WORKER_TAILSCALE_TAG"))

    logger.info(
        f"Existing workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in existing_workers]}")
    logger.info(
        f"Tailscale workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in tailscale_workers]}")

    for worker in tailscale_workers:
      existing_worker = next((w for w in existing_workers if w.hostname == worker.hostname), None)
      if existing_worker:
        if existing_worker.online and not worker.online:
          # worker went offline
          logger.info(f"Worker {worker.hostname} went offline.")
          self.set_worker_offline(worker)
        elif not existing_worker.online and worker.online:
          # worker came back online
          logger.info(f"Worker {worker.hostname} came back online.")
          self.set_worker_online(worker)

        if existing_worker.ip != worker.ip:
          # worker IP changed (worker was re-created)
          logger.info(
              f"Worker {
                  worker.hostname} IP changed from {
                  existing_worker.ip} to {
                  worker.ip}.")
          self.setup_worker(worker)
      else:
        # new worker
        logger.info(f"New worker {worker.hostname} detected.")
        self.setup_worker(worker)

    for existing_worker in existing_workers:
      if (not any(w.hostname == existing_worker.hostname for w in tailscale_workers)
              and existing_worker.online):
        # worker went offline or was removed
        logger.info(
            f"Worker {
                existing_worker.hostname} not found in tailscale. Presumed it went offline.")
        self.set_worker_offline(existing_worker)

  def setup_worker(self, worker):
    """
    Setup a new worker -
    - Add to Manager database
    - Create Cloudflare DNS entry (*.int) for Tailscale routing
    - Fetch a Cloudflare Tunnel Token
    - Sync Compose, Traefik, Cloudflared files
    - Start Compose
    """
    logger.info(f"Adding worker {worker.hostname} to manager.")
    try:
      domain = Settings().get("cloudflare", "domain")
      admin_email = Settings().get("cloudflare", "admin_email")

      Worker.insert(hostname=worker.hostname, ip=worker.ip).on_conflict(
          conflict_target=[Worker.hostname], preserve=[Worker.ip]
      ).execute()
      self.cloudflare.create_dns_record(
          name=f"*.int.{domain}",
          content=worker.ip,
          comment=f"sage-worker-{worker.hostname}",
          type="A",
      )
      tunnel = self.cloudflare.get_tunnel_token()

      # Sync files
      self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/docker-compose.yml",
          f"{self.worker_home_dir}/docker-compose.yml",
      )
      self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/worker.env",
          f"{self.worker_home_dir}/.env",
          {
              "SAGE_HOME": self.worker_home_dir,
              "TS_IP": worker.ip,
              "TS_HOSTNAME": worker.hostname,
              "TUNNEL_TOKEN": tunnel.token,
          },
      )
      self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/traefik/traefik.yml",
          f"{self.worker_home_dir}/traefik/traefik.yml",
          {"ADMIN_EMAIL": admin_email},
      )
      self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/traefik/config.yml",
          f"{self.worker_home_dir}/traefik/dynamic/config.yml",
          {"DOMAIN": domain, "HOSTNAME": worker.hostname},
      )
      self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/vector/vector.yml",
          f"{self.worker_home_dir}/vector/config/vector.yml",
          {"IP": self.tailscale.ip(), "HOSTNAME": worker.hostname},
      )

      # Start containers
      self.tailscale.exec_command(
          worker.hostname,
          f"docker compose -f {self.worker_home_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build",
          timeout=300,
      )

      Worker.update(online=True).where(
          Worker.hostname == worker.hostname).execute()

      # Trigger Traefik update config for all applications having containers on this worker
      Application.update(
          domains_synced=False).where(
          Application.id.in_(
              Container.select(
                  Container.application_id).where(
                  Container.worker_id == worker.hostname))).execute()

      self.notify(f"Worker {worker.hostname} added.")
    except Exception as e:
      # Cleanup on failure
      self.notify(f"Failed to setup worker {worker.hostname} : {e}", "error")
      self.remove_worker(worker)
      raise Exception(f"Failed to setup worker {worker.hostname} : {e}")

  def remove_worker(self, worker):
    """
    A removed worker is assumed to have been destoryed externally (VM + Tailscale) -
    - Remove from Manager database
    - Delete Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare automatically cleans up dead tunnels
    """
    logger.info(f"Removing worker {worker.hostname} from manager.")
    Worker.delete().where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.delete_dns_record(
        name=f"*.int.{Settings().get('cloudflare', 'domain')}",
        content=worker.ip,
    )
    logger.info(f"Worker {worker.hostname} removed from manager.")

  def set_worker_offline(self, worker):
    """
    A worker can be set to offline if it is expected to come back soon, e.g. after a reboot -
    - Mark as offline in Manager database
    - Delete Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare will automatically handle an offline tunnel
    """
    logger.info(f"Setting worker {worker.hostname} offline.")
    Worker.update(online=False).where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.delete_dns_record(
        name=f"*.int.{Settings().get('cloudflare', 'domain')}",
        content=worker.ip,
    )
    self.notify(f"Worker {worker.hostname} is offline.", "error")

  def set_worker_online(self, worker):
    """
    - Mark as online in Manager database
    - Create Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare will automatically handle tunnel re-connection
    """
    logger.info(f"Setting worker {worker.hostname} online.")
    Worker.update(online=True).where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.create_dns_record(
        name=f"*.int.{Settings().get('cloudflare', 'domain')}",
        content=worker.ip,
        comment=f"sage-worker-{worker.hostname}",
        type="A",
    )
    self.notify(f"Worker {worker.hostname} is back online.", "success")

  async def update_workers_config(self, admin_email_changed=False, domain_changed=False):
    # No updates
    if not admin_email_changed and not domain_changed:
      return

    domain = Settings().get("cloudflare", "domain")
    admin_email = Settings().get("cloudflare", "admin_email")

    # Update Manager Traefik
    self.traefik.load(clear_certificates=domain_changed)
    await self.traefik.sync_certificates_to_workers()

    # Update Worker Traefik config
    online_workers = list(Worker.select().where(Worker.online))
    for worker in online_workers:
      if admin_email_changed:
        await run_in_executor_with_context(
            self.tailscale.sync_file,
            worker.hostname,
            app_dir / "templates/worker/traefik/traefik.yml",
            f"{self.worker_home_dir}/traefik/traefik.yml",
            {"ADMIN_EMAIL": admin_email},
        )

      if domain_changed:
        await run_in_executor_with_context(
            self.tailscale.sync_file,
            worker.hostname,
            app_dir / "templates/worker/traefik/config.yml",
            f"{self.worker_home_dir}/traefik/dynamic/config.yml",
            {"DOMAIN": domain, "HOSTNAME": worker.hostname},
        )

        await run_in_executor_with_context(
            self.cloudflare.create_dns_record,
            f"*.int.{domain}",
            worker.ip,
            comment=f"sage-worker-{worker.hostname}",
            type="A",
        )

      if admin_email_changed or domain_changed:
        await run_in_executor_with_context(
            self.tailscale.exec_command,
            worker.hostname,
            f"docker compose -f {self.worker_home_dir}/docker-compose.yml restart traefik",
            timeout=60,
        )

    # Trigger Traefik update config for all applications to update the domain in the routing rules
    if domain_changed:
      Application.update(domains_synced=False).execute()

  def is_volume_backup_due(self, volume: Volume, now: datetime) -> bool:
    if not volume.backup_cron:
      return False

    try:
      period = Cron(*volume.backup_cron.split())
    except Exception as exc:
      logger.error(f"Invalid backup cron for application {volume.application.project.name}-{volume.application.name} volume {volume.name}: {exc}")
      return False

    if now not in period:
      return False

    window = period.rollback(now)
    return not Backup.select().where(
        (Backup.type == "application")
        & (Backup.application == volume.application)
        & (Backup.source_volume_name == volume.name)
        & (Backup.created_at >= window.left)
        & (Backup.created_at < window.right)
    ).exists()

  def get_volume_backup_resource_error(
      self,
      application: Application,
      volumes: list[Volume],
  ) -> str | None:
    project_application_name = f"{application.project.name}-{application.name}"

    if application.container_count == 0:
      return f"Application {project_application_name} has no containers to back up."

    if not volumes:
      return f"Application {project_application_name} has no volumes to back up."

    if any(volume.application_id != application.id for volume in volumes):
      return f"One or more selected volumes do not belong to application {project_application_name}."

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

  def sync_application_status(self):
    """
    Sync Application & Container status from the workers.
    Ideally status is managed explicitly so this is to catch unexpected changes
    like a container being stopped from the worker side or a worker going offline without Manager knowing yet.
    """
    # Get all application containers
    containers = list(Container.select())

    # Get the container status from each worker
    container_status = {}
    workers = list(Worker.select())
    for worker in workers:
      if worker.online:
        try:
          _, docker_ps_output = self.tailscale.exec_command(
              worker.hostname, "docker ps --format '{{.Names}}|{{.State}}'")
          for line in docker_ps_output:
            try:
              container_name, container_state = line.split("|")
            except Exception:
              continue
            container_status[f"{worker.hostname}-{container_name}"] = container_state
        except Exception as e:
          # If the command fails, assume worker is offline
          logger.error(f"Failed to get container status from worker {worker.hostname} while syncing application status: {e}")

    for container in containers:
      # Skip state update during explicit actions
      if container.status in APPLICATION_BUSY_STATUSES:
        continue

      worker_container_name = f"{container.worker.hostname}-{container.application.project.name}-{container.application.name}"
      status = container_status.get(worker_container_name)
      if status:
        if status == "running" and container.status != "active":
          container.status = "active"
          container.save()
          self.notify(f"Application container {worker_container_name} is active again.", "success")
        elif status in ["paused", "restarting"] and container.status != "error":
          container.status = "error"
          container.save()
          self.notify(f"Application container {worker_container_name} is in error state ({status}).", "error")
      else:
        # if no status is found and container is supposed to be active, mark as
        # error (could be offline or stopped container)
        if container.status == "active":
          container.status = "error"
          container.save()
          self.notify(f"Application container {worker_container_name} is in error state (status not found).", "error")

    # Sync the overall application status
    applications = Application.select()
    for application in applications:
      if application.status in APPLICATION_BUSY_STATUSES:
        continue

      containers = list(application.containers)
      project_application_name = f"{application.project.name}-{application.name}"

      if any(c.status == "error" for c in containers):
        if application.status != "error":
          application.status = "error"
          application.save()
          self.notify(
              f"Application {project_application_name} is in error state as at least one container is in error state.",
              "error")

      elif all(c.status == "active" for c in containers):
        if application.status != "active":
          application.status = "active"
          application.save()
          self.notify(
              f"Application {project_application_name} is active as all containers are active.",
              "success")

      elif all(c.status == "inactive" for c in containers):
        if application.status != "inactive":
          application.status = "inactive"
          application.save()
          self.notify(
              f"Application {project_application_name} is inactive as all containers are inactive.",
              "warning")

  def sync_application_traefik_domains_config(self, application: Application):
    """
    Sync Traefik domains config for an application.
    """
    container_name = f"{application.project.name}-{application.name}"
    domain_name = Settings().get("cloudflare", "domain")
    containers = list(application.containers)
    application_domains = list(application.domains)
    workers = list(Worker.select())
    online_workers = [worker for worker in workers if worker.online]
    active_containers = [
        container
        for container in containers
        if container.worker.online and container.status == "active"
    ]
    logger.info(
        f"Syncing Traefik config for application {container_name} for these workers {[container.worker.hostname for container in active_containers]}."
    )

    # Preserve the full declared tag list for discovery, even when a tag has no
    # currently active backing container.
    domain_tags = sorted({container.domain_tag for container in containers if container.domain_tag})
    domain_tag_pools = {
        tag: [container for container in active_containers if container.domain_tag == tag]
        for tag in domain_tags
    }

    # Reset any config on online workers before either clearing stale configs or
    # writing the new routing view for this application.
    for worker in workers:
      if not worker.online:
        logger.error(f"Worker {worker.hostname} is offline, skipping Traefik config sync for application {container_name} on this worker.")
        continue

      self.tailscale.exec_command(
          worker.hostname,
          f"rm -f {self.worker_home_dir}/traefik/dynamic/{container_name}-*.yml",
      )

    # Application is deploying or stopping.
    if application.status in APPLICATION_BUSY_STATUSES:
      logger.error(f"Application {container_name} is not active, skipping Traefik config sync.")
      return

    template_names = {
        "internal": ("service_internal.yml", "service_internal_pool.yml"),
        "public": ("service_public.yml", "service_public_pool.yml"),
    }
    # Mesh communication between Traefik uses port 9002 (HTTP). For the same
    # worker, use traefik instead of the Tailscale IP to avoid hairpinning and
    # let Docker DNS resolve locally.
    load_balanced_servers = {
        worker.hostname: ", ".join(
            (
                '{ url: "http://traefik:9002" }'
                if worker.hostname == container.worker.hostname
                else f'{{ url: "http://{container.worker.ip}:9002" }}'
            )
            for container in active_containers
        )
        for worker in online_workers
    }
    domain_tag_load_balanced_servers = {
        worker.hostname: {
            domain_tag: ", ".join(
                (
                    '{ url: "http://traefik:9002" }'
                    if worker.hostname == container.worker.hostname
                    else f'{{ url: "http://{container.worker.ip}:9002" }}'
                )
                for container in domain_tag_pool
            )
            for domain_tag, domain_tag_pool in domain_tag_pools.items()
        }
        for worker in online_workers
    }

    # Application is active -> sync Traefik config based on active containers
    # to all online workers (mesh network).
    for worker in online_workers:
      for domain in application_domains:
        traefik_config_template, traefik_config_template_domain_pool = template_names[
            domain.type
        ]
        self.tailscale.sync_file(
            worker.hostname,
            app_dir / f"templates/worker/traefik/{traefik_config_template}",
            f"{self.worker_home_dir}/traefik/dynamic/{container_name}-{domain.type}-{domain.name}.yml",
            {
                "SERVICE": container_name,
                "SUBDOMAIN": domain.name,
                "DOMAIN": domain_name,
                "DOMAIN_TAGS_HEADER": json.dumps(domain_tags),
                "PORT": domain.port,
                "LOAD_BALANCED_SERVERS": load_balanced_servers[worker.hostname],
            },
        )

        # Create Domain Tag pool config.
        for domain_tag in domain_tags:
          self.tailscale.sync_file(
              worker.hostname,
              app_dir / f"templates/worker/traefik/{traefik_config_template_domain_pool}",
              f"{self.worker_home_dir}/traefik/dynamic/{container_name}-{domain.type}-{domain.name}-{domain_tag}.yml",
              {
                  "SERVICE": container_name,
                  "SUBDOMAIN": domain.name,
                  "DOMAIN": domain_name,
                  "POOL_TAG": domain_tag,
                  "LOAD_BALANCED_SERVERS": domain_tag_load_balanced_servers[worker.hostname][domain_tag],
              },
          )

    for domain in application_domains:
      domain_url = (
          f"https://{domain.name}.int.{domain_name}"
          if domain.type == "internal"
          else f"https://{domain.name}.{domain_name}"
      )
      self.notify(
          f"Traefik config synced for application {container_name} with domain {domain.type}:{domain.name}.",
          link=domain_url,
      )

    application.domains_synced = True
    application.save()
    logger.info(f"Traefik config synced for application {container_name}.")

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
        f"{application.project.name}-{application.name} "
        f"on worker {container.worker.hostname} volume {volume.name}"
    )

  def _get_backup_timestamp_from_key(self, key: str) -> str | None:
    filename = key.strip("/").split("/")[-1]
    match = re.search(r"\d{8}_\d{6}", filename)
    if not match:
      return None
    return match.group(0)

  def _is_expired_backup_timestamp(self, timestamp_str: str, cutoff: datetime) -> bool:
    try:
      return datetime.strptime(timestamp_str, self.backup_timestamp_format) < cutoff
    except Exception:
      return False

  def _is_expired_backup_key(self, key: str, cutoff: datetime) -> bool:
    timestamp_str = self._get_backup_timestamp_from_key(key)
    if not timestamp_str:
      return False
    return self._is_expired_backup_timestamp(timestamp_str, cutoff)

  async def _stop_container_for_backup(self, container: Container):
    if container.status != "active":
      return

    container_name = f"{container.application.project.name}-{container.application.name}"
    container_dir = f"{self.worker_home_dir}/applications/{container.application.name}"
    logger.info(
        f"Stopping application {container_name} container on worker {container.worker.hostname} for backup."
    )
    await run_in_executor_with_context(
        self.tailscale.exec_command,
        container.worker.hostname,
        f"docker compose -f {container_dir}/docker-compose.yml down",
    )

  async def _start_container_after_backup(self, container: Container):
    container_name = f"{container.application.project.name}-{container.application.name}"
    container_dir = f"{self.worker_home_dir}/applications/{container.application.name}"
    logger.info(
        f"Starting application {container_name} container on worker {container.worker.hostname} after backup."
    )

    try:
      await run_in_executor_with_context(
          self.tailscale.exec_command,
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
      await run_in_executor_with_context(
          self.tailscale.sync_file,
          container.worker.hostname,
          app_dir / "templates/worker/application/backup.sh",
          remote_script_path,
          {
              "WORKER_HOME_DIR": self.worker_home_dir,
              "APP_NAME": application.name,
              "VOLUME_NAME": volume.name,
              "ARCHIVE_NAME": archive_name,
              "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
              "UPLOAD_URL": upload_url,
          })
      await run_in_executor_with_context(
          self.tailscale.exec_command,
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

  async def backup_application_s3(self, application: Application, volume_ids: list[int] | None = None):
    application = Application.get_by_id(application.id)
    project_application_name = f"{application.project.name}-{application.name}"

    if application.status not in BACKUP_ELIGIBLE_STATUSES:
      raise Exception(
          f"Application {project_application_name} must be active or inactive before backup."
      )

    snapshot = self._get_application_backup_snapshot(application)
    timestamp = datetime.now().strftime(self.backup_timestamp_format)
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
            f"Failed backup units for {project_application_name}: {'; '.join(failed_backup_units)}"
        )

      await self._restore_application_runtime_after_backup(application, snapshot)
    except Exception:
      try:
        await self._restore_application_runtime_after_backup(application, snapshot)
      except Exception as restore_exc:
        logger.error(
            f"Failed to restore application {project_application_name} to its previous state after backup failure: {restore_exc}")
        application.status = "error"
        application.save()
        for entry in snapshot["containers"]:
          container = Container.get_by_id(entry["id"])
          if container.status in ["backup", "restoring"]:
            container.status = "error"
            container.save()
      raise

  async def restore_application_volume_from_s3(
      self,
      application: Application,
      volume: Volume,
      backup: Backup,
      target_worker_hostname: str,
  ):
    application = Application.get_by_id(application.id)
    volume = Volume.get_by_id(volume.id)
    backup = Backup.get_by_id(backup.id)
    project_application_name = f"{application.project.name}-{application.name}"

    if application.status != "inactive":
      raise ValueError(
          f"Application {project_application_name} must be inactive before restore."
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
          f"Target worker {target_worker_hostname} is not attached to application {project_application_name}."
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
      await run_in_executor_with_context(
          self.tailscale.sync_file,
          target_container.worker.hostname,
          app_dir / "templates/worker/application/restore.sh",
          remote_script_path,
          {
              "WORKER_HOME_DIR": self.worker_home_dir,
              "APP_NAME": application.name,
              "VOLUME_NAME": volume.name,
              "ENCRYPTION_KEY": get_env("ENCRYPTION_KEY"),
              "DOWNLOAD_URL": download_url,
          },
      )
      await run_in_executor_with_context(
          self.tailscale.exec_command,
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
          f"Failed to restore {restore_unit_label} from {backup.s3_path}: {exc}",
          "error",
      )
      raise Exception(f"Failed to restore {restore_unit_label}: {exc}") from exc
    finally:
      self._restore_application_backup_snapshot(application, snapshot)
      if task_id_token is not None:
        task_id.reset(task_id_token)

  async def deploy_application(self, application: Application):
    """
    Deploy an application.
    """
    application.status = "deploying"
    application.save()
    project_application_name = f"{application.project.name}-{application.name}"
    logger.info(f"Deploying application {project_application_name}...")

    await asyncio.gather(
        *[self.deploy_application_container(container)
          for container in application.containers],
        return_exceptions=False,
    )

    if any(container.status == "error" for container in application.containers):
      application.status = "error"
      application.save()
      self.notify(
          f"Failed to deploy application {project_application_name}.",
          "error")
      raise Exception(f"Failed to deploy application {project_application_name}.")
    else:
      application.status = "active"
      application.save()
      self.notify(f"Application {project_application_name} deployed.", "success")

  async def deploy_application_container(self, container: Container):
    # Create an event for tracking with a different task id.
    container_task_id = generate_task_id_token()
    Event.create(
        container=container,
        type="deploy",
        application_task_id=task_id.get(),
        container_task_id=container_task_id,
    )
    container.status = "deploying"
    container.save()
    container_name = f"{container.application.project.name}-{container.application.name}"
    container_dir = f"{self.worker_home_dir}/applications/{container.application.name}"
    logger.info(
        f"Deploying application {container_name} container to worker {
            container.worker.hostname} with task id {container_task_id}...")

    exception_message = None

    try:
      task_id_token = task_id.set(container_task_id)

      app_env = container.application.env if container.application.env else ""
      app_build_args = container.application.args if container.application.args else ""
      app_build_args = (
          [
              f"{{ {build_arg.split('=')[0]}: \"{build_arg.split('=')[1]}\" }}"
              for build_arg in app_build_args.split("\n")
          ]
          if app_build_args
          else []
      )

      # Create the secrets file
      await run_in_executor_with_context(
          self.tailscale.sync_file,
          container.worker.hostname,
          app_dir / "templates/worker/file",
          f"{container_dir}/.env",
          {"CONTENT": app_env},
      )

      # Create the volumes
      volumes = list(container.application.volumes)
      volumes_config = [
          f"\"{container_dir}/volumes/{v.name}:{v.path}\""
          for v in volumes
      ]
      volume_mkdir_cmd = ';'.join([f"mkdir -p {container_dir}/volumes/{v.name}" for v in volumes])
      await run_in_executor_with_context(
          self.tailscale.exec_command,
          container.worker.hostname,
          volume_mkdir_cmd
      )

      # Get existing volumes on worker which are not in the current config and need to be cleaned up
      _, existing_volumes = await run_in_executor_with_context(
          self.tailscale.exec_command,
          container.worker.hostname,
          f"ls -1 {container_dir}/volumes || true",
      )
      volumes_to_cleanup = set(existing_volumes) - set(v.name for v in volumes)
      if volumes_to_cleanup:
        volume_cleanup_cmd = ';'.join([f"rm -rf {container_dir}/volumes/{v}" for v in volumes_to_cleanup])
        await run_in_executor_with_context(
            self.tailscale.exec_command,
            container.worker.hostname,
            volume_cleanup_cmd
        )

      # Create the compose file based on application type
      if container.application.type == "docker":
        await run_in_executor_with_context(
            self.tailscale.sync_file,
            container.worker.hostname,
            app_dir / "templates/worker/application/dockerhub-compose.yml",
            f"{container_dir}/docker-compose.yml",
            {
                "APPLICATION_NAME": container.application.name,
                "CONTAINER_NAME": container_name,
                "IMAGE": container.application.image,
                "VOLUMES": " ,".join(volumes_config),
            },
        )
      elif container.application.type == "git":
        await run_in_executor_with_context(
            self.tailscale.sync_file,
            container.worker.hostname,
            app_dir / "templates/worker/application/gitrepo-compose.yml",
            f"{container_dir}/docker-compose.yml",
            {
                "APPLICATION_NAME": container.application.name,
                "CONTAINER_NAME": container_name,
                "REPO": container.application.repo,
                "DOCKERFILE": container.application.path,
                "BUILD_ARGS": " ,".join(app_build_args),
                "VOLUMES": " ,".join(volumes_config),
            },
        )

      # Deploy with docker compose
      await run_in_executor_with_context(
          self.tailscale.exec_command,
          container.worker.hostname,
          f"docker compose -f {container_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build",
      )

      deployment_status = "active"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "active":
      self.notify(f"Application {container_name} container deployed to worker {container.worker.hostname}.")
    else:
      self.notify(
          f"Failed to deploy application {container_name} container to worker {container.worker.hostname}: {exception_message}", "error")

  def delete_container(self, container: Container, force: bool = False):
    """
    Delete a container.
    """
    # Create an event for tracking in case of error.
    Event.create(
        container=container,
        type="delete",
        application_task_id=task_id.get(),
        container_task_id=task_id.get(),
    )
    container.status = "stopping"
    container.save()

    container_dir = f"{self.worker_home_dir}/applications/{container.application.name}"
    container_name = f"{container.application.project.name}-{container.application.name}"

    logger.info(
        f"Deleting container of application {container_name} from worker {
            container.worker.hostname}")

    try:
      skip_remote_cleanup = force and not container.worker.online

      if skip_remote_cleanup:
        logger.warning(
            f"Force deleting container of application {container_name} from offline worker {
                container.worker.hostname}; skipping remote cleanup.")
      else:
        # Stop container on worker
        self.tailscale.exec_command(
            container.worker.hostname,
            f'[ ! -d "{container_dir}" ] || docker compose -f "{container_dir}/docker-compose.yml" down --volumes --rmi all --remove-orphans',
            timeout=60,
        )

        # Remove application folder
        self.tailscale.exec_command(
            container.worker.hostname, f"rm -rf {container_dir}", timeout=30
        )

        # Remove traefik config
        self.tailscale.exec_command(
            container.worker.hostname,
            f"rm -rf {self.worker_home_dir}/traefik/dynamic/{container_name}-*.yml",
            timeout=30,
        )

      # Delete database record
      container.delete_instance()

      if skip_remote_cleanup:
        self.notify(
            f"Container of application {container_name} force-deleted from offline worker {
                container.worker.hostname}. Remote cleanup skipped.",
            "warning",
        )
      else:
        self.notify(
            f"Container of application {container_name} deleted from worker {
                container.worker.hostname}.", "success")
    except Exception as e:
      container.status = "error"
      container.save()
      self.notify(
          f"Failed to delete container of application {container_name} from worker {
              container.worker.hostname}: {e}", "error")
      raise Exception(
          f"Failed to delete container {
              container.id} of application {container_name} from worker {
              container.worker.hostname}: {e}")

  async def stop_application(self, application: Application):
    """
    Stop an application.
    """
    application.status = "stopping"
    application.save()
    project_application_name = f"{application.project.name}-{application.name}"
    logger.info(f"Stopping application {project_application_name}...")

    await asyncio.gather(
        *[self.stop_application_container(container) for container in application.containers],
        return_exceptions=False,
    )

    if any(container.status == "error" for container in application.containers):
      application.status = "error"
      application.save()
      self.notify(f"Failed to stop application {project_application_name}.", "error")
      raise Exception(f"Failed to stop application {project_application_name}.")
    else:
      application.status = "inactive"
      application.save()
      self.notify(f"Application {project_application_name} stopped.", "success")

  async def stop_application_container(self, container: Container):
    # Create an event for tracking with a different task id.
    container_task_id = generate_task_id_token()
    container_name = f"{container.application.project.name}-{container.application.name}"
    was_inactive = container.status == "inactive"
    Event.create(
        container=container,
        type="stop",
        application_task_id=task_id.get(),
        container_task_id=container_task_id,
    )
    container.status = "stopping"
    container.save()
    logger.info(
        f"Stopping application {container_name} container on worker {
            container.worker.hostname} with task id {container_task_id}...")

    exception_message = None

    try:
      task_id_token = task_id.set(container_task_id)
      container_dir = f"{self.worker_home_dir}/applications/{container.application.name}"

      if was_inactive:
        logger.info(
            f"Container of application {container_name} on worker {
                container.worker.hostname} is already inactive. Skipping compose down.")
      else:
        # Stop with docker compose
        await run_in_executor_with_context(
            self.tailscale.exec_command,
            container.worker.hostname,
            f"docker compose -f {container_dir}/docker-compose.yml down",
        )

      deployment_status = "inactive"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "inactive":
      self.notify(
          f"Application {container_name} container stopped on worker {container.worker.hostname}.")
    else:
      self.notify(
          f"Failed to stop application {container_name} container on worker {container.worker.hostname}: {exception_message}", "error")

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

  async def restore_database_from_s3(self, s3_path: str):
    """
    Restore database from S3 backup.

    Process:
    1. Download backup file from S3 to temporary location
    2. Validate it's a valid SQLite database
    3. Create safety backup of current database
    4. Restore into the live database using SQLite's online backup API
    5. Verify database integrity
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

    except Exception as e:
      self.notify(f"Platform restore failed: {e}", "error")
      raise
