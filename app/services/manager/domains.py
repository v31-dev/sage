import logging

from services.db import APPLICATION_BUSY_STATUSES, Application, Domain, db
from services.domains import ROUTING_TEMPLATES, DomainConfig, domains_changed
from services.updates import Fact, UpdateSource
from utils.common import get_env, utcnow
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict

from ._common import DOMAIN_ROUTING_TEMPLATES, app_dir
from .application import application_changed

logger = logging.getLogger(__name__)


def domain_changed(domain: Domain, previous_container=None):
  domains_changed()
  for application in {container.application for container in (domain.container, previous_container) if container}:
    application_changed(application)


def save_domain(domain: Domain):
  if domain.dirty_fields:
    domain.save()
    domain_changed(domain)


class DomainsMixin:
  def domain_config(self, domain: Domain) -> DomainConfig:
    return DomainConfig.capture(domain, get_env("DOMAIN"), DOMAIN_ROUTING_TEMPLATES)

  def domain_tasks_busy(self) -> frozenset:
    snapshot = self.task_snapshot()
    return frozenset((task["params"]["domain_id"], task["params"]["application_id"])
                     for task in (*snapshot["running"], *snapshot["queued"]) if task["name"] == "sync_domain")

  def domains_queue_facts(self) -> list[Fact]:
    previous = frozenset()

    def changed(current):
      nonlocal previous
      applications = {application for _, application in current ^ previous if application is not None}
      previous = current
      for application in Application.select().where(Application.id.in_(applications)):
        application_changed(application)

    return [Fact(self.domain_tasks_busy, UpdateSource.DOMAINS, on_change=changed)]

  def domain_status(self, domain: Domain) -> str:
    return "syncing" if self.has_task(self.sync_domain, {"domain_id": domain.id}) else domain.status

  def request_domain_sync(self, domain: Domain):
    with db.atomic():
      domain = Domain.get_or_none(Domain.id == domain.id)
      if domain is None:
        return
      if domain.status != "pending" or domain.sync_error is not None:
        domain.status = "pending"
        domain.sync_error = None
        save_domain(domain)
      snapshot = self.domain_config(domain)
    scopes = {f"domain:{snapshot.domain_id}"}
    if snapshot.application is not None:
      scopes.add(f"app:{snapshot.application}")
    self.add_task(
        task=self.sync_domain, scopes=scopes, executor="app", on_conflict=OnConflict.REPLACE, quiet=True,
        params={"domain_id": snapshot.domain_id, "application_id": snapshot.application_id},
    )

  def request_domain_syncs(self, domains):
    for domain in domains:
      self.request_domain_sync(domain)

  async def sync_domain(self, domain_id: int, application_id: int | None):
    with db.atomic():
      domain = Domain.get_or_none(Domain.id == domain_id)
      if domain is None:
        logger.info(f"Domain {domain_id} deleted before sync; nothing to do.")
        return
      snapshot = self.domain_config(domain)
      if snapshot.application_id != application_id:
        self.request_domain_sync(domain)
        return
      if domain.container and domain.container.status in APPLICATION_BUSY_STATUSES:
        self.request_application_status_sync(domain.container.application)
        return

    error = None
    pending = snapshot.container_id is not None and not self.is_worker_online(snapshot.worker)
    phase = "Traefik"
    try:
      if not pending:
        dynamic = f"{self.worker_home_dir}/traefik/dynamic"
        if snapshot.active:
          await self.tailscale.sync_file(
              snapshot.worker, app_dir / f"templates/worker/traefik/{ROUTING_TEMPLATES[snapshot.type]}",
              f"{dynamic}/{snapshot.filename}", {
                  "ID": snapshot.domain_id, "HOST": snapshot.host, "SERVICE": snapshot.application, "PORT": snapshot.port,
              })
        else:
          workers = [snapshot.worker] if snapshot.container_id is not None else [w.hostname for w in self.online_workers()]
          for worker in workers:
            await self.tailscale.exec_command(worker, f"rm -f {dynamic}/{snapshot.filename}")

        phase = "DNS"
        await run_in_executor_with_context(self.cloudflare.connect)
        records = {}
        if snapshot.type == "public" and snapshot.container_id is not None:
          tunnel = await run_in_executor_with_context(
              self.cloudflare.find_tunnel, self.cloudflare.worker_tunnel_name(snapshot.worker))
          if tunnel is None:
            raise ValueError(f"Worker {snapshot.worker} has no tunnel.")
          records[snapshot.host] = f"{tunnel['id']}.cfargotunnel.com"
        await run_in_executor_with_context(
            self.cloudflare.sync_dns_records, {snapshot.dns_comment: records}, type="CNAME", proxied=True)
    except Exception as failure:
      error = failure
      logger.error(f"Domain {domain_id} sync failed at {phase}: {failure}")

    repeat = False
    with db.atomic():
      domain = Domain.get_or_none(Domain.id == domain_id)
      if domain is not None:
        repeat = self.domain_config(domain).content_hash != snapshot.content_hash
        status = "pending" if repeat or pending else "error" if error else "synced"
        message = f"{phase}: {error}" if error and not repeat else None
        if domain.status != status or domain.sync_error != message or status == "synced":
          domain.status = status
          domain.sync_error = message
          if status == "synced":
            domain.synced_hash = snapshot.content_hash
            domain.synced_at = utcnow()
          save_domain(domain)
    if repeat:
      self.request_domain_sync(domain)
    self.request_discovery_sync()
    if error:
      raise error
