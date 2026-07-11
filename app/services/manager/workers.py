import logging

from services.db import (
    Application,
    Container,
    Worker,
)
from services.settings import Settings
from utils.common import get_env
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed

from ._common import app_dir

logger = logging.getLogger(__name__)


class WorkersMixin:
  async def sync_workers(self, force: bool = False):
    """
    Check for worker changes.
    - New workers (not in db but in tailscale)
    - Offline workers (in db but tailscale says offline)
    - Updated workers (IPs have changed)
    - Recently online workers (previously offline but now back online)
    - Offline workers (in db but not in tailscale)
    """
    existing_workers = list(Worker.select())
    tailscale_workers = await run_in_executor_with_context(
        self.tailscale.get_by_tag, get_env("WORKER_TAILSCALE_TAG"))

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
            await run_in_executor_with_context(self.set_worker_offline, worker)
          elif not existing_worker.online and worker.online:
            # worker came back online
            logger.info(f"Worker {worker.hostname} came back online.")
            await run_in_executor_with_context(self.set_worker_online, worker)

          if existing_worker.ip != worker.ip:
            # worker IP changed (worker was re-created)
            logger.info(
                f"Worker {
                    worker.hostname} IP changed from {
                    existing_worker.ip} to {
                    worker.ip}.")
            await self.setup_worker(worker)
          elif force:
            # worker is the same but force re-sync requested
            logger.info(f"Force syncing worker {worker.hostname}.")
            await self.setup_worker(worker)
        else:
          # new worker
          logger.info(f"New worker {worker.hostname} detected.")
          await self.setup_worker(worker)
      except Exception as e:
        logger.error(f"sync_workers failed for {worker.hostname}: {e}")

    for existing_worker in existing_workers:
      try:
        if (not any(w.hostname == existing_worker.hostname for w in tailscale_workers)
                and existing_worker.online):
          # worker went offline or was removed
          logger.info(
              f"Worker {existing_worker.hostname} not found in tailscale. Presumed it went offline.")
          await run_in_executor_with_context(self.set_worker_offline, existing_worker)
      except Exception as e:
        logger.error(f"sync_workers offline-check failed for {existing_worker.hostname}: {e}")

  async def setup_worker(self, worker):
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
      await run_in_executor_with_context(
          self.cloudflare.create_dns_record,
          name=f"*.int.{domain}",
          content=worker.ip,
          comment=f"sage-worker-{worker.hostname}",
          type="A",
      )
      tunnel = await run_in_executor_with_context(self.cloudflare.get_tunnel_token)

      # Sync files
      await self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/docker-compose.yml",
          f"{self.worker_home_dir}/docker-compose.yml",
      )
      await self.tailscale.sync_file(
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
      await self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/traefik/traefik.yml",
          f"{self.worker_home_dir}/traefik/traefik.yml",
          {"ADMIN_EMAIL": admin_email},
      )
      await self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/traefik/config.yml",
          f"{self.worker_home_dir}/traefik/dynamic/config.yml",
          {"DOMAIN": domain, "HOSTNAME": worker.hostname},
      )
      await self.tailscale.sync_file(
          worker.hostname,
          app_dir / "templates/worker/vector/vector.yml",
          f"{self.worker_home_dir}/vector/config/vector.yml",
          {"IP": self.tailscale.ip(), "HOSTNAME": worker.hostname},
      )

      # Start containers
      await self.tailscale.exec_command(
          worker.hostname,
          f"docker compose -f {self.worker_home_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build",
          timeout=300,
      )

      Worker.update(online=True).where(
          Worker.hostname == worker.hostname).execute()

      # Sync the wildcard cert to the worker when the manager has a valid one.
      if self.traefik.has_valid_certificates():
        await self.traefik.sync_certificates_to_worker(worker)

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
        await run_in_executor_with_context(self.remove_worker, worker.hostname)
      raise Exception(f"Failed to {action} worker {worker.hostname} : {e}")

  def remove_worker(self, worker_hostname: str):
    """
    A removed worker is assumed to have been destoryed externally (VM + Tailscale) -
    - Remove from Manager database
    - Delete Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare automatically cleans up dead tunnels
    """
    worker = Worker.get_or_none(Worker.hostname == worker_hostname)
    if not worker:
      return

    # Re-check under the scope lock: a deploy could have placed a container here
    # between the route's check and this task running. Fail loudly so it surfaces
    # in the task table rather than vanishing.
    if worker.containers.count() > 0:
      logger.error(f"Worker {worker_hostname} gained containers before removal; aborting.")
      raise TaskFailed()

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
