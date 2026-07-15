import json
import logging
import re

from services.db import Application, Domain, Project, Worker
from services.settings import Settings
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict

from ._common import app_dir

logger = logging.getLogger(__name__)


class TraefikMixin:
  def request_application_traefik_sync(self, application):
    """Mark the app's Traefik config stale and enqueue an app-scoped resync."""
    Application.update(domains_synced=False).where(Application.id == application.id).execute()
    self.add_task(
        task=self.sync_application_traefik_domains_config,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id},
        executor="app",
        on_conflict=OnConflict.REPLACE,
        quiet=True,
    )

  async def sync_traefik_certificates(self):
    await self.traefik.sync_certificates_to_workers()

  async def refresh_traefik(self, admin_email_changed=False, domain_changed=False, api_token_changed=False):
    # No updates
    if not admin_email_changed and not domain_changed and not api_token_changed:
      return

    try:
      # Refresh Manager Traefik static config (ADMIN_EMAIL), dynamic config (DOMAIN),
      # and the Cloudflare DNS API token file, then restart the container so the
      # static config and token are re-read by lego at provider init.
      self.traefik.load(clear_certificates=domain_changed)
      await run_in_executor_with_context(self.restart, traefik=True)

      # Token-only rotations don't need worker churn — workers don't run ACME,
      # they only receive synced acme.json from the manager.
      if not admin_email_changed and not domain_changed:
        return

      domain = Settings().get("cloudflare", "domain")
      admin_email = Settings().get("cloudflare", "admin_email")

      await self.traefik.sync_certificates_to_workers()

      # Update Worker Traefik config
      online_workers = list(Worker.select().where(Worker.online))
      for worker in online_workers:
        if admin_email_changed:
          await self.tailscale.sync_file(
              worker.hostname,
              app_dir / "templates/worker/traefik/traefik.yml",
              f"{self.worker_home_dir}/traefik/traefik.yml",
              {"ADMIN_EMAIL": admin_email},
          )

        if domain_changed:
          await self.tailscale.sync_file(
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
          await self.tailscale.exec_command(
              worker.hostname,
              f"docker compose -f {self.worker_home_dir}/docker-compose.yml restart traefik",
              timeout=60,
          )

      # Trigger a Traefik resync for every application so the new domain lands in the routing rules.
      if domain_changed:
        for application in Application.select():
          self.request_application_traefik_sync(application)
    except Exception as e:
      self.notify(
          f"Traefik refresh failed part-way: {e}. Run 'Resync Traefik' in Settings "
          "to bring the manager and workers back in sync.",
          "error")
      raise

  async def sync_application_traefik_domains_config(self, application_id: int):
    """
    Sync Traefik domains config for an application. Applications with no active containers
    have config written for X-Tag discovery.
    """
    application = Application.get_or_none(Application.id == application_id)
    if application is None:
      # The reconcile scan owns file cleanup for deleted applications.
      logger.info(f"Application {application_id} deleted before Traefik sync; nothing to do.")
      return
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
        f"Syncing Traefik config for application {
            application.qualified_name} for these workers {
            [
                container.worker.hostname for container in active_containers]}."
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
        logger.error(
            f"Worker {
                worker.hostname} is offline, skipping Traefik config sync for application {
                application.qualified_name} on this worker.")
        continue

      await self.tailscale.exec_command(
          worker.hostname,
          f"rm -f {self.worker_home_dir}/traefik/dynamic/{application.qualified_name}-*.yml",
      )

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
    # TCP backing services mesh over port 9003 (address form, not URL). The edge
    # passthrough router load-balances across every active backing worker, just like
    # the HTTP mesh; same-worker uses traefik to avoid hairpinning.
    tcp_load_balanced_servers = {
        worker.hostname: ", ".join(
            (
                '{ address: "traefik:9003" }'
                if worker.hostname == container.worker.hostname
                else f'{{ address: "{container.worker.ip}:9003" }}'
            )
            for container in active_containers
        )
        for worker in online_workers
    }

    # Application is active -> sync Traefik config based on active containers
    # to all online workers (mesh network).
    for worker in online_workers:
      for domain in application_domains:
        # TCP services route by SNI and have no x-tag pool variant (TCP carries no
        # query string), so they use a dedicated single-template path.
        if domain.type == "tcp":
          await self.tailscale.sync_file(
              worker.hostname,
              app_dir / "templates/worker/traefik/service_internal_tcp.yml",
              f"{self.worker_home_dir}/traefik/dynamic/{application.qualified_name}-{domain.type}-{domain.name}.yml",
              {
                  "SERVICE": application.qualified_name,
                  "SUBDOMAIN": domain.name,
                  "DOMAIN": domain_name,
                  "PORT": domain.port,
                  "LOAD_BALANCED_SERVERS": tcp_load_balanced_servers[worker.hostname],
              },
          )

        # HTTP services can route by path and support x-tag based pool splitting,
        # so they use the standard template pair.
        else:
          traefik_config_template, traefik_config_template_domain_pool = template_names[
              domain.type
          ]
          await self.tailscale.sync_file(
              worker.hostname,
              app_dir / f"templates/worker/traefik/{traefik_config_template}",
              f"{self.worker_home_dir}/traefik/dynamic/{application.qualified_name}-{domain.type}-{domain.name}.yml",
              {
                  "SERVICE": application.qualified_name,
                  "SUBDOMAIN": domain.name,
                  "DOMAIN": domain_name,
                  "DOMAIN_TAGS_HEADER": json.dumps(domain_tags),
                  "PORT": domain.port,
                  "LOAD_BALANCED_SERVERS": load_balanced_servers[worker.hostname],
              },
          )

          # Create Domain Tag pool config.
          for domain_tag in domain_tags:
            await self.tailscale.sync_file(
                worker.hostname,
                app_dir / f"templates/worker/traefik/{traefik_config_template_domain_pool}",
                f"{self.worker_home_dir}/traefik/dynamic/{application.qualified_name}-{domain.type}-{domain.name}-{domain_tag}.yml",
                {
                    "SERVICE": application.qualified_name,
                    "SUBDOMAIN": domain.name,
                    "DOMAIN": domain_name,
                    "POOL_TAG": domain_tag,
                    "LOAD_BALANCED_SERVERS": domain_tag_load_balanced_servers[worker.hostname][domain_tag],
                },
            )

    for domain in application_domains:
      if domain.type == "tcp":
        domain_url = f"{domain.name}.int.{domain_name}:8443"
      elif domain.type == "internal":
        domain_url = f"https://{domain.name}.int.{domain_name}"
      else:
        domain_url = f"https://{domain.name}.{domain_name}"
      self.notify(
          f"Traefik config synced for application {application.qualified_name} with domain {domain.type}:{domain.name}.",
          link=domain_url,
      )

    application.domains_synced = True
    application.save()
    logger.info(f"Traefik config synced for application {application.qualified_name}.")

  async def reconcile_traefik_configs(self):
    """
    Existence-level reconciliation between worker Traefik dynamic configs and
    applications: a file with no owning application is removed, and an
    application with domains must have config on every online worker. Content
    correctness is owned by sync_application_traefik_domains_config;
    applications left domains_synced=False (e.g. a restart dropped their queued
    sync) are re-enqueued here.
    """
    online_workers = list(Worker.select().where(Worker.online))
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
          if name.endswith(".yml") and name != "config.yml"  # worker's own config
          and re.fullmatch(r"[a-z0-9.-]+", name)             # rm-safe names only
          and not any(name.startswith(prefix) for prefix in live_prefixes)
      )
      if orphans:
        logger.info(f"Removing orphaned Traefik configs on {worker.hostname}: {orphans}")
        await self.tailscale.exec_command(
            worker.hostname,
            ";".join(f"rm -f {self.worker_home_dir}/traefik/dynamic/{name}" for name in orphans),
        )

    applications_ids_with_domains = {
        domain.application_id for domain in Domain.select(Domain.application).distinct()
    }

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

      # An app with domains must have config on every online worker; a missing
      # file means the worker joined after the app's last sync.
      if application.id not in applications_ids_with_domains:
        continue
      prefix = f"{application.qualified_name}-"
      if any(
          not any(name.startswith(prefix) for name in worker_files[worker.hostname])
          for worker in online_workers
      ):
        self.request_application_traefik_sync(application)
