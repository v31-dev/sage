import asyncio
import logging

import config
from services.db import Domain, Worker
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict
from utils.retry import is_transient, retry

from ._common import DISCOVERY_TEMPLATES, app_dir, content_hash

logger = logging.getLogger(__name__)


class TraefikMixin:
  def request_certificate_renewal(self):
    self.add_task(
        task=self.renew_certificates,
        scopes={"platform", "app"},
        executor="platform",
        on_conflict=OnConflict.REPLACE,
    )

  def request_discovery_sync(self):
    self.add_task(
        task=self.sync_discovery,
        scopes={"domain"},
        executor="app",
        on_conflict=OnConflict.REPLACE,
        quiet=True,
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

  def discovery_services(self) -> list:
    """Each service, as `[namespace, service, [[host, pool], ...]]`, sorted, with the names linked to a container in
    it: a public service holds public names, an internal one internal and TCP names."""
    services = {}
    for domain in Domain.select().where(Domain.service.is_null(False) & Domain.container.is_null(False) & (Domain.status == "synced")):
      namespace = "public" if domain.type == "public" else "internal"
      services.setdefault((namespace, domain.service), []).append([domain.host(self.cloudflare.domain), domain.pool])
    return [[namespace, service, sorted(names, key=lambda name: name[0])]
            for (namespace, service), names in sorted(services.items())]

  def discovery_hash(self, services: list) -> str:
    """Hash of everything the discovery files of `services` are rendered from: the `discovery` stamp."""
    return content_hash({"templates": DISCOVERY_TEMPLATES, "domain": self.cloudflare.domain, "services": services})

  async def sync_discovery(self):
    """Write, to every online worker, a discovery router per service, so any worker answers from its files alone:
    `discovery.<domain>?service=<service>` for a public one (through the platform's tunnel) and
    `discovery.int.<domain>?service=<service>` for an internal one (tailnet only). HEAD, GET and OPTIONS get the
    service's names and pools in `Sage-Endpoints`; another method gets a 404. Each worker's stale files go last, and its
    `discovery` stamp says what it has."""
    services = self.discovery_services()
    stamp = self.discovery_hash(services)
    hosts = {"public": f"discovery.{self.cloudflare.domain}", "internal": f"discovery.int.{self.cloudflare.domain}"}
    dynamic = f"{self.worker_home_dir}/traefik/dynamic"
    for worker in self.online_workers():
      written = []
      for namespace, service, names in services:
        file = f"_discovery-{namespace}-{service}.yml"
        await self.tailscale.sync_file(
            worker.hostname, app_dir / f"templates/worker/traefik/discovery_{namespace}.yml", f"{dynamic}/{file}", {
                "SERVICE": service,
                "HOST": hosts[namespace],
                # An RFC 9651 list: each name a string, its pool a parameter.
                "ENDPOINTS": ", ".join(f'"{host}";pool="{pool}"' if pool else f'"{host}"' for host, pool in names),
            })
        written.append(file)
      await self.tailscale.exec_command(
          worker.hostname,
          f"find {dynamic} -maxdepth 1 -name '_discovery-*.yml' " + "".join(f"! -name '{file}' " for file in written)
          + "-delete",
      )
      await self.write_worker_revision(worker.hostname, "discovery", stamp)
    await self.sync_discovery_records()
    logger.info(f"Discovery synced: {len(services)} service(s).")

  async def sync_discovery_records(self):
    """The public discovery name and its internal addresses on online workers."""
    zone = await run_in_executor_with_context(self.cloudflare.get_dns_records)
    domain_name = self.cloudflare.domain
    await run_in_executor_with_context(self.cloudflare.sync_dns_records, {
        "sage-tunnel": {f"discovery.{domain_name}": f"{self.cloudflare.tunnel_id}.cfargotunnel.com"},
    }, zone=zone, type="CNAME", proxied=True)
    # A worker no longer in Sage has its record deleted too.
    discovery = {record["comment"]: {} for record in zone if (record.get("comment") or "").startswith("sage-discovery-")}
    for worker in Worker.select():
      discovery[f"sage-discovery-{worker.hostname}"] = (
          {f"discovery.int.{domain_name}": worker.ip} if self.is_worker_online(worker.hostname) else {})
    await run_in_executor_with_context(self.cloudflare.sync_dns_records, discovery, zone=zone, type="A")
