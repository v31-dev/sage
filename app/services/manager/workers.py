import logging

from services.db import (
    APPLICATION_BUSY_STATUSES,
    Application,
    Container,
    Worker,
)
from services.settings import Settings
from utils.common import get_env

from ._common import app_dir

logger = logging.getLogger(__name__)


class WorkersMixin:
  def sync_workers(self, force: bool = False):
    """
    Check for worker changes.
    - New workers (not in db but in tailscale)
    - Offline workers (in db but tailscale says offline)
    - Updated workers (IPs have changed)
    - Recently online workers (previously offline but now back online)
    - Offline workers (in db but not in tailscale)
    """
    existing_workers = list(Worker.select())
    tailscale_workers = self.tailscale.get_by_tag(get_env("WORKER_TAILSCALE_TAG"))

    logger.info(
        f"Existing workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in existing_workers]}")
    logger.info(
        f"Tailscale workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in tailscale_workers]}")

    for worker in tailscale_workers:
      try:
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
          elif force:
            # worker is the same but force re-sync requested
            logger.info(f"Force syncing worker {worker.hostname}.")
            self.setup_worker(worker)
        else:
          # new worker
          logger.info(f"New worker {worker.hostname} detected.")
          self.setup_worker(worker)
      except Exception as e:
        logger.error(f"sync_workers failed for {worker.hostname}: {e}")

    for existing_worker in existing_workers:
      try:
        if (not any(w.hostname == existing_worker.hostname for w in tailscale_workers)
                and existing_worker.online):
          # worker went offline or was removed
          logger.info(
              f"Worker {existing_worker.hostname} not found in tailscale. Presumed it went offline.")
          self.set_worker_offline(existing_worker)
      except Exception as e:
        logger.error(f"sync_workers offline-check failed for {existing_worker.hostname}: {e}")

  def setup_worker(self, worker):
    """
    Setup or re-sync a worker -
    - Add to Manager database
    - Create Cloudflare DNS entry (*.int) for Tailscale routing
    - Fetch a Cloudflare Tunnel Token
    - Sync Compose, Traefik, Cloudflared files
    - Start Compose

    Safe to call against an existing worker for re-sync. On failure, the worker
    row + DNS are only cleaned up if this call was adding a brand-new worker.
    """
    is_new = not Worker.select().where(Worker.hostname == worker.hostname).exists()
    logger.info(f"{'Adding' if is_new else 'Re-syncing'} worker {worker.hostname}.")
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

      self.notify(f"Worker {worker.hostname} {'added' if is_new else 'synced'}.")
    except Exception as e:
      action = "setup" if is_new else "sync"
      self.notify(f"Failed to {action} worker {worker.hostname} : {e}", "error")
      # Only roll back the worker row + DNS if this was a fresh add.
      if is_new:
        self.remove_worker(worker)
      raise Exception(f"Failed to {action} worker {worker.hostname} : {e}")

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

  def sync_application_status(self, application_id: int):
    """
    Sync a single application's container & overall status from its workers.
    Ideally status is managed explicitly; this catches unexpected changes like
    a container stopped from the worker side or a worker going offline without
    the Manager knowing yet.
    """
    application = Application.get_by_id(application_id)

    # The queue scope keeps this off an app with an operation running, but guard
    # defensively against any explicit-action status.
    if application.status in APPLICATION_BUSY_STATUSES:
      return

    containers = list(application.containers)
    if not containers:
      return

    # Query container state only on the (distinct, online) workers backing this
    # application's containers.
    container_status = {}
    workers = {container.worker.hostname: container.worker for container in containers}
    for hostname, worker in workers.items():
      if not worker.online:
        continue
      try:
        _, docker_ps_output = self.tailscale.exec_command(
            hostname, "docker ps --format '{{.Names}}|{{.State}}'")
        for line in docker_ps_output:
          try:
            container_name, container_state = line.split("|")
          except Exception:
            continue
          container_status[f"{hostname}-{container_name}"] = container_state
      except Exception as e:
        logger.error(
            f"Failed to get container status from worker {hostname} while syncing application {application.qualified_name}: {e}")

    for container in containers:
      # Skip state update during explicit actions
      if container.status in APPLICATION_BUSY_STATUSES:
        continue

      worker_container_name = f"{container.worker.hostname}-{application.qualified_name}"
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

    # Sync the overall application status from the (possibly updated) containers.
    if application.status in APPLICATION_BUSY_STATUSES:
      return

    containers = list(application.containers)

    if any(c.status == "error" for c in containers):
      if application.status != "error":
        application.status = "error"
        application.save()
        self.notify(
            f"Application {application.qualified_name} is in error state as at least one container is in error state.",
            "error")

    elif all(c.status == "active" for c in containers):
      if application.status != "active":
        application.status = "active"
        application.save()
        self.notify(
            f"Application {application.qualified_name} is active as all containers are active.",
            "success")

    elif all(c.status == "inactive" for c in containers):
      if application.status != "inactive":
        application.status = "inactive"
        application.save()
        self.notify(
            f"Application {application.qualified_name} is inactive as all containers are inactive.",
            "warning")
