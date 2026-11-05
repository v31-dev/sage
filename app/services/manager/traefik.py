import asyncio
import logging

import config
from services.db import Domain, Worker
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict
from utils.retry import is_transient, retry

from ._common import app_dir

logger = logging.getLogger(__name__)


class TraefikMixin:
  def request_certificate_renewal(self):
    self.add_task(
        task=self.renew_certificates,
        scopes={"platform", "app", "domain"},
        executor="platform",
        on_conflict=OnConflict.REPLACE,
    )

  def setup_domain(self):
    """Before Sage serves: connect to Cloudflare (zone, tunnel, the manager's record) and
    make sure the manager has its certificate, retrying a cold start before giving up.
    Blocking; raises when the domain can't be set up."""
    logger.info(
        f"Setting up {self.cloudflare.manager_hostname}: Cloudflare (zone, tunnel, DNS record), then the "
        "manager's certificate. The UI starts once this is done; a first start can take a few minutes.")
    try:
      retry(
          self.cloudflare.connect, delays=config.STARTUP_RETRY_DELAYS,
          should_retry=lambda error: is_transient(error) and not self.certs.manager.is_valid(),
          on_retry=lambda error, delay: logger.warning(f"Cloudflare setup failed during startup: {error}. Retrying in {delay}s."))
    except Exception as e:
      # With a certificate that still works, serve now; Cloudflare is retried on use.
      if not self.certs.manager.is_valid():
        raise
      logger.error(f"Cloudflare setup failed during startup: {e}. Continuing on the current certificate.")
      return
    self.certs.ensure_manager_at_startup()

  async def renew_certificates(self):
    """Issue each certificate that is missing or within its renewal window, hot-reload
    the manager's :443 server in place, then push the workers' certificate to any worker
    whose stamp is stale."""
    if await run_in_executor_with_context(self.certs.ensure, self.certs.manager):
      self.certs.reload_local_tls()
      self.notify(f"Renewed the manager's certificate; expires {self.certs.manager.expiry()}.", "success")
    if await run_in_executor_with_context(self.certs.ensure, self.certs.workers):
      self.update_workers({})
      self.notify(f"Renewed the workers' certificate; expires {self.certs.workers.expiry()}.", "success")
    await self.sync_certificates_to_workers()

  async def refresh_traefik(self):
    """Ensure both certificates are valid and resync everything that depends on them —
    the manager's :443 server, each worker's PEM, Traefik config and `*.<subdomain>.int` DNS
    record, and app routing. Triggered by Resync Traefik."""
    try:
      if await run_in_executor_with_context(self.certs.ensure, self.certs.manager):
        self.certs.reload_local_tls()
      if await run_in_executor_with_context(self.certs.ensure, self.certs.workers):
        self.update_workers({})

      domain = self.cloudflare.domain
      await self.sync_certificates_to_workers(force=True)

      # Worker Traefik picks up config.yml from its watched dynamic dir (file
      # provider), so no restart is needed.
      for worker in self.online_workers():
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/traefik/config.yml",
            f"{self.worker_home_dir}/traefik/dynamic/config.yml",
            {"DOMAIN": domain, "HOSTNAME": worker.hostname},
        )
        await run_in_executor_with_context(self.cloudflare.sync_worker_records, worker.hostname, worker.subdomain, worker.ip)

      self.request_domain_syncs(Domain.select())
      self.request_discovery_sync()
    except Exception as e:
      self.notify(
          f"Traefik refresh failed part-way: {e}. Run Resync Traefik on the Cloudflare settings page "
          "to bring the manager and workers back in sync.",
          "error")
      raise

  async def sync_certificates_to_workers(self, force: bool = False):
    """Push the workers' certificate to every online worker whose stamp is stale, or
    to all of them when forced."""
    if not self.certs.workers.is_valid():
      self.notify(
          "No valid certificate available; skipping certificate sync to workers.",
          type="error",
      )
      return

    cert_hash = self.certs.workers.hash()
    workers = self.online_workers()
    if force:
      targets = workers
    else:
      revisions = await asyncio.gather(
          *[self.read_worker_revisions(worker.hostname) for worker in workers])
      targets = [
          worker for worker, revision in zip(workers, revisions)
          if revision.get("certs") != cert_hash
      ]

    # Workers are independent, so sync them concurrently: the asyncssh SFTP
    # leaves fan out and drain on the event loop.
    await asyncio.gather(
        *[self.sync_certificates_to_worker(worker) for worker in targets],
        return_exceptions=False,
    )

  async def sync_certificates_to_worker(self, worker: Worker):
    traefik_dir = f"{self.worker_home_dir}/traefik"
    await self.tailscale.sync_file(
        worker.hostname, self.certs.workers.fullchain_path, f"{traefik_dir}/certs/fullchain.pem")
    await self.tailscale.sync_file(
        worker.hostname, self.certs.workers.key_path, f"{traefik_dir}/certs/key.pem", mode=0o600)
    # Re-write the watched dynamic cert config so Traefik's file-provider watch
    # fires and reloads the new PEM; overwriting only the cert files is not a
    # reliable reload trigger.
    await self.tailscale.sync_file(
        worker.hostname,
        app_dir / "templates/worker/traefik/certs.yml",
        f"{traefik_dir}/dynamic/certs.yml")
    await self.write_worker_revision(
        worker.hostname, "certs", self.certs.workers.hash() or "")
    self.update_worker(worker.hostname, certs=self.certs.workers.hash())
    logger.info(f"Synced certificate to worker {worker.hostname}.")
