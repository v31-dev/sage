import asyncio
import logging
import re

import config
from services.db import Application, Domain, Project, Worker
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict
from utils.retry import is_transient, retry

from ._common import DISCOVERY_TEMPLATES, app_dir, content_hash, routing_input_hash
from .application import application_changed, save_application

logger = logging.getLogger(__name__)


class TraefikMixin:
  def request_application_traefik_sync(self, application):
    """Mark the app's Traefik config stale and enqueue an app-scoped resync."""
    Application.update(domains_synced=False).where(Application.id == application.id).execute()
    application_changed(application)
    self.add_task(
        task=self.sync_application_traefik_domains_config,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id},
        executor="app",
        on_conflict=OnConflict.REPLACE,
        quiet=True,
    )

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
        scopes={"platform"},
        executor="platform",
        on_conflict=OnConflict.REPLACE,
        quiet=True,
    )

  def request_domain_records_sync(self):
    self.add_task(
        task=self.sync_domain_records,
        scopes={"platform"},
        executor="platform",
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

      # Resync every application so the new domain lands in the routing rules.
      for application in Application.select():
        self.request_application_traefik_sync(application)
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

  async def sync_application_traefik_domains_config(self, application_id: int):
    """
    Write, to each online worker that runs an active container of the application, a file per domain linked to that
    container, routing the domain's name to it, and remove the application's other files on every online worker,
    last, so every state in between routes. Then ask for the public names' records.
    """
    application = Application.get_or_none(Application.id == application_id)
    if application is None:
      # The reconcile scan owns file cleanup for deleted applications.
      logger.info(f"Application {application_id} deleted before Traefik sync; nothing to do.")
      return
    domain_name = self.cloudflare.domain
    containers = list(application.containers)
    online_workers = self.online_workers()
    hosting = {container.worker.hostname: container for container in containers
               if self.is_worker_online(container.worker.hostname) and container.status == "active"}
    logger.info(f"Syncing Traefik config for application {application.qualified_name} for these workers {sorted(hosting)}.")

    for worker in Worker.select():
      if not self.is_worker_online(worker.hostname):
        logger.error(
            f"Worker {worker.hostname} is offline, skipping Traefik config sync for application "
            f"{application.qualified_name} on this worker.")

    templates = {"public": "service_public.yml", "internal": "service_internal.yml", "tcp": "service_internal_tcp.yml"}
    dynamic = f"{self.worker_home_dir}/traefik/dynamic"
    for worker in online_workers:
      written = []
      if worker.hostname in hosting:
        for domain in hosting[worker.hostname].domains:
          file = f"{application.qualified_name}-{domain.type}-{domain.name}.yml"
          await self.tailscale.sync_file(
              worker.hostname, app_dir / f"templates/worker/traefik/{templates[domain.type]}", f"{dynamic}/{file}", {
                  "SERVICE": application.qualified_name,
                  "NAME": domain.name,
                  "HOST": domain.host(domain_name),
                  "PORT": domain.port,
              })
          written.append(file)
      await self.tailscale.exec_command(
          worker.hostname,
          f"find {dynamic} -maxdepth 1 -name '{application.qualified_name}-*.yml' "
          + "".join(f"! -name '{file}' " for file in written) + "-delete",
      )

    domains = list(application.domains)
    input_hash = routing_input_hash(domain_name, domains, containers, self.is_worker_online)
    for worker in online_workers:
      await self.write_worker_revision(worker.hostname, application.qualified_name, input_hash)

    for domain in domains:
      self.notify(
          f"Traefik config synced for application {application.qualified_name} with domain {domain.type}:{domain.name}.",
          link=domain.address(domain_name),
      )

    self.request_domain_records_sync()
    application.domains_synced = True
    save_application(application)
    logger.info(f"Traefik config synced for application {application.qualified_name}.")

  def discovery_services(self) -> list:
    """Each service, as `[namespace, service, [[host, pool], ...]]`, sorted, with the names linked to a container in
    it: a public service holds public names, an internal one internal and TCP names."""
    services = {}
    for domain in Domain.select().where(Domain.service.is_null(False) & Domain.container.is_null(False)):
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
    logger.info(f"Discovery synced: {len(services)} service(s).")

  async def sync_domain_records(self):
    """Make the zone's records for what Sage serves: `discovery.<domain>` a CNAME to the platform's tunnel
    (`sage-tunnel`), each public name linked to a container a CNAME to the tunnel of that container's worker
    (`sage-domain`), whatever the container's state, so a name doesn't come and go with deploys, and
    `discovery.int.<domain>` an A record to each online worker (`sage-discovery-<hostname>`), so internal discovery
    goes round the workers that answer. The one writer of these records, from one list of the account's tunnels and
    one of the zone's records."""
    tunnels = await run_in_executor_with_context(self.cloudflare.tunnels)
    zone = await run_in_executor_with_context(self.cloudflare.get_dns_records)
    domain_name = self.cloudflare.domain
    names = {}
    for domain in Domain.select().where((Domain.type == "public") & Domain.container.is_null(False)):
      tunnel = tunnels.get(self.cloudflare.worker_tunnel_name(domain.container.worker_id))
      if tunnel is None:
        logger.warning(f"Worker {domain.container.worker_id} has no tunnel yet; {domain.host(domain_name)} waits for "
                       "its setup.")
        continue
      names[domain.host(domain_name)] = f"{tunnel['id']}.cfargotunnel.com"
    await run_in_executor_with_context(self.cloudflare.sync_dns_records, {
        "sage-tunnel": {f"discovery.{domain_name}": f"{self.cloudflare.tunnel_id}.cfargotunnel.com"},
        "sage-domain": names,
    }, zone=zone, type="CNAME", proxied=True)
    # A worker no longer in Sage has its record deleted too.
    discovery = {record["comment"]: {} for record in zone if (record.get("comment") or "").startswith("sage-discovery-")}
    for worker in Worker.select():
      discovery[f"sage-discovery-{worker.hostname}"] = (
          {f"discovery.int.{domain_name}": worker.ip} if self.is_worker_online(worker.hostname) else {})
    await run_in_executor_with_context(self.cloudflare.sync_dns_records, discovery, zone=zone, type="A")

  async def reconcile_traefik_configs(self):
    """
    Existence-level reconciliation between worker Traefik dynamic configs and
    applications: a file with no owning application is removed, and an
    application with domains must have config on every online worker that runs
    an active container of it. Content correctness is owned by
    sync_application_traefik_domains_config; applications left
    domains_synced=False (e.g. a restart dropped their queued sync) are
    re-enqueued here.
    """
    online_workers = self.online_workers()
    worker_files = {}
    for worker in online_workers:
      _, files = await self.tailscale.exec_command(
          worker.hostname,
          f"ls -1 {self.worker_home_dir}/traefik/dynamic 2>/dev/null || true",
      )
      worker_files[worker.hostname] = set(files)

    # Applications are created by route writes that can interleave with this
    # task; listing files first means any file listed above has its owner
    # present in this read, so a fresh app is never misclassified as an orphan.
    applications = list(Application.select(Application, Project).join(Project))
    live_prefixes = {f"{application.qualified_name}-" for application in applications}

    for worker in online_workers:
      orphans = sorted(
          name for name in worker_files[worker.hostname]
          if name.endswith(".yml")
          and name not in ("config.yml", "certs.yml")        # worker's own infra configs
          and re.fullmatch(r"[a-z0-9.-]+", name)             # rm-safe names only
          and not any(name.startswith(prefix) for prefix in live_prefixes)
      )
      if orphans:
        logger.info(f"Removing orphaned Traefik configs on {worker.hostname}: {orphans}")
        await self.tailscale.exec_command(
            worker.hostname,
            ";".join(f"rm -f {self.worker_home_dir}/traefik/dynamic/{name}" for name in orphans),
        )

    # Revision stamps: existence checks above catch missing files; the hash
    # layer catches files rendered from stale inputs (a worker that missed a
    # sync). Stamps for deleted applications are removed here, like their
    # routing files; the worker's own stamps (infra, certs, owner, discovery) stay.
    revisions_by_worker = {}
    for worker in online_workers:
      revisions_by_worker[worker.hostname] = await self.read_worker_revisions(worker.hostname)
    live_names = {application.qualified_name for application in applications}
    for worker in online_workers:
      stale_keys = sorted(
          key for key in revisions_by_worker[worker.hostname]
          if key not in live_names and key not in ("infra", "certs", "owner", "discovery")
          and re.fullmatch(r"[a-z0-9-]+", key)
      )
      if stale_keys:
        await self.tailscale.exec_command(
            worker.hostname,
            ";".join(f"rm -f {self.worker_home_dir}/revisions/{key}" for key in stale_keys),
        )

    domain_name = self.cloudflare.domain
    for application in applications:
      if not application.domains_synced:
        self.add_task(
            task=self.sync_application_traefik_domains_config,
            scopes={f"app:{application.qualified_name}"},
            params={"application_id": application.id},
            executor="app",
            quiet=True,
        )
        continue

      # An online worker whose active container of the app has domains must have its files; a missing one means the
      # worker missed the app's last sync.
      prefix = f"{application.qualified_name}-"
      needs_sync = any(
          not any(name.startswith(prefix) for name in worker_files[container.worker_id])
          for container in application.containers
          if container.status == "active" and container.worker_id in worker_files and container.domains.exists()
      )

      if not needs_sync and online_workers:
        app_hash = routing_input_hash(
            domain_name, list(application.domains), list(application.containers), self.is_worker_online)
        needs_sync = any(
            revisions_by_worker[worker.hostname].get(application.qualified_name) != app_hash
            for worker in online_workers
        )

      if needs_sync:
        self.request_application_traefik_sync(application)

    if online_workers:
      discovery = self.discovery_hash(self.discovery_services())
      if any(revisions_by_worker[worker.hostname].get("discovery") != discovery for worker in online_workers):
        self.request_discovery_sync()
