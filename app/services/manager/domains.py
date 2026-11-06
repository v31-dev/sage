import asyncio
import json
import logging
import shlex

import config
from services.cloudflare import dns_record_matches
from services.db import APPLICATION_BUSY_STATUSES, Application, Container, Domain, db
from services.domains import ROUTING_TEMPLATES, DomainConfig, domain_artifact_owner, domains_changed
from services.updates import Fact, Updates, UpdateSource
from utils.common import get_env, utcnow
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict

from ._common import DISCOVERY_TEMPLATES, DOMAIN_ROUTING_TEMPLATES, app_dir, content_hash
from .application import application_changed

logger = logging.getLogger(__name__)


def domain_changed(domain: Domain, previous_container=None):
  domains_changed()
  for application in {container.application for container in (domain.container, previous_container) if container}:
    application_changed(application)
  for hostname in {container.worker_id for container in (domain.container, previous_container) if container}:
    Updates().changed(UpdateSource.WORKER_CONTAINERS, hostname)


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
      affected = current ^ previous
      applications = {application for _, application in affected if application is not None}
      previous = current
      for application in Application.select().where(Application.id.in_(applications)):
        application_changed(application)
      hostnames = {container.worker_id for container in Container.select(Container.worker).join(Domain).where(
          Domain.id.in_({domain_id for domain_id, _ in affected}))}
      for hostname in hostnames:
        Updates().changed(UpdateSource.WORKER_CONTAINERS, hostname)

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
                  "CORS_ORIGINS": json.dumps(snapshot.cors_origins),
              })
        else:
          workers = [snapshot.worker] if snapshot.container_id is not None else [w.hostname for w in self.online_workers()]
          for worker in workers:
            await self.tailscale.exec_command(worker, f"rm -f {dynamic}/{snapshot.filename}")

        phase = "DNS"
        await run_in_executor_with_context(self.cloudflare.connect)
        tunnel = None
        if snapshot.type == "public" and snapshot.container_id is not None:
          tunnel = await run_in_executor_with_context(
              self.cloudflare.find_tunnel, self.cloudflare.worker_tunnel_name(snapshot.worker))
        await run_in_executor_with_context(
            self.cloudflare.sync_dns_records, {snapshot.dns_comment: snapshot.dns_records(tunnel)}, type="CNAME", proxied=True)
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
    self.request_discovery_sync(repair=False)
    if error:
      self.notify(f"Domain {snapshot.host or domain_id} sync failed at {phase}: {error}", "error", "/domains")
      raise error

  def request_discovery_sync(self, repair: bool = True):
    # A child holds its domain scope: its DEDUP follow-up cannot meet a running parent or replace a queued full scan.
    self.add_task(
        task=self.sync_discovery, scopes={"domain"}, executor="app", params={"repair": repair},
        on_conflict=OnConflict.REPLACE if repair else OnConflict.DEDUP, quiet=True,
    )

  def discovery_services(self, domains=None, ready=None) -> list:
    """Services as `[namespace, service, [[host, pool, online], ...]]` from configured records."""
    if ready is None:
      ready = self.ready_workers()
    services = {}
    for domain in Domain.select() if domains is None else domains:
      if domain.service is None:
        continue
      snapshot = self.domain_config(domain)
      if snapshot.host is None:
        continue
      online = (snapshot.active and snapshot.worker in ready and domain.status == "synced"
                and domain.synced_hash == snapshot.content_hash)
      namespace = "public" if domain.type == "public" else "internal"
      services.setdefault((namespace, domain.service), []).append([snapshot.host, domain.pool, online])
    return [[namespace, service, sorted(names, key=lambda name: name[0])]
            for (namespace, service), names in sorted(services.items())]

  def discovery_hash(self, services: list) -> str:
    return content_hash({"templates": DISCOVERY_TEMPLATES, "domain": self.cloudflare.domain, "services": services,
                         "cache_seconds": config.DISCOVERY_CACHE_SECONDS})

  async def sync_discovery(self, repair: bool = True):
    workers = self.online_workers()
    dynamic = f"{self.worker_home_dir}/traefik/dynamic"
    failures = []
    zone, tunnels = None, None
    try:
      await run_in_executor_with_context(self.cloudflare.connect)
      if repair:
        zone, tunnels = await asyncio.gather(
            run_in_executor_with_context(self.cloudflare.get_dns_records),
            run_in_executor_with_context(self.cloudflare.tunnels), return_exceptions=True)
      else:
        zone, = await asyncio.gather(run_in_executor_with_context(self.cloudflare.get_dns_records), return_exceptions=True)
      if isinstance(zone, Exception):
        failures.append(f"DNS inventory: {zone}")
        zone = None
      if isinstance(tunnels, Exception):
        failures.append(f"Tunnel inventory: {tunnels}")
        tunnels = None
    except Exception as error:
      failures.append(f"Cloudflare: {error}")

    listings = await asyncio.gather(*[
        self.tailscale.exec_command(
            worker.hostname, f"if [ -d {dynamic} ]; then find {dynamic} -maxdepth 1 -type f -printf '%f\\n'; fi", timeout=30)
        for worker in workers], return_exceptions=True)
    revisions = await asyncio.gather(*[self.read_worker_revisions(worker.hostname, timeout=30) for worker in workers], return_exceptions=True)
    files, stamps = {}, {}
    for worker, listing, revision in zip(workers, listings, revisions):
      if isinstance(listing, Exception):
        failures.append(f"{worker.hostname} files: {listing}")
      else:
        files[worker.hostname] = set(listing[1])
      if isinstance(revision, Exception):
        failures.append(f"{worker.hostname} revisions: {revision}")
      else:
        stamps[worker.hostname] = revision

    # Inventory precedes owners: instant creates must be visible before an artifact is classified as orphaned.
    with db.atomic():
      ready = self.ready_workers()
      domains = {domain.id: domain for domain in Domain.select()}
      configs = {domain.id: self.domain_config(domain) for domain in domains.values()}
      busy = {domain.id for domain in domains.values() if domain.container and (
          domain.container.status in APPLICATION_BUSY_STATUSES or domain.container.application.status in APPLICATION_BUSY_STATUSES)}

    if repair:
      for domain in domains.values():
        if domain.id in busy:
          continue
        snapshot = configs[domain.id]
        drift = domain.status != "synced" or domain.synced_hash != snapshot.content_hash
        if snapshot.container_id is not None and snapshot.worker in files:
          drift |= (snapshot.filename in files[snapshot.worker]) != snapshot.active
        if zone is not None and (tunnels is not None or snapshot.type != "public" or snapshot.container_id is None):
          records = [record for record in zone if record.get("comment") == snapshot.dns_comment]
          try:
            wanted = snapshot.dns_records((tunnels or {}).get(self.cloudflare.worker_tunnel_name(snapshot.worker)))
            drift |= len(records) != len(wanted) or any(
                record["name"] not in wanted or not dns_record_matches(record, {
                    "content": wanted[record["name"]], "type": "CNAME", "proxied": True}) for record in records)
          except ValueError:
            drift = True
        if drift:
          self.request_domain_sync(domain)
          domain.status = "pending"

    confirmed = {domain.id for domain in domains.values() if domain.id not in busy and domain.status == "synced"
                 and domain.synced_hash == configs[domain.id].content_hash and (not configs[domain.id].active or
                 configs[domain.id].filename in files.get(configs[domain.id].worker, set()))}
    services = self.discovery_services(domains.values(), ready=ready)
    stamp = self.discovery_hash(services)
    expected = {f"_discovery-{namespace}-{service}.yml" for namespace, service, _ in services}
    hosts = {"public": f"discovery.{self.cloudflare.domain}", "internal": f"discovery.int.{self.cloudflare.domain}"}
    for worker in workers:
      if worker.hostname not in files:
        continue
      obsolete = []
      for name in files[worker.hostname]:
        domain_id = domain_artifact_owner("file", name)
        if domain_id is None:
          continue
        if domain_id not in domains or (domain_id in confirmed and (
                configs[domain_id].container_id is None or not configs[domain_id].active or configs[domain_id].worker != worker.hostname)):
          obsolete.append(name)
      try:
        if obsolete:
          await self.tailscale.exec_command(
              worker.hostname, "rm -f " + " ".join(shlex.quote(f"{dynamic}/{name}") for name in sorted(obsolete)))
        actual = {name for name in files[worker.hostname] if name.startswith("_discovery-") and name.endswith(".yml")}
        if actual == expected and stamps.get(worker.hostname, {}).get("discovery") == stamp:
          continue
        for namespace, service, names in services:
          await self.tailscale.sync_file(
              worker.hostname, app_dir / f"templates/worker/traefik/discovery_{namespace}.yml",
              f"{dynamic}/_discovery-{namespace}-{service}.yml", {
                  "SERVICE": service, "HOST": hosts[namespace], "CACHE_SECONDS": config.DISCOVERY_CACHE_SECONDS,
                  "ENDPOINTS": ", ".join(f'"{host}"' + (f';pool="{pool}"' if pool else "")
                                         + (";online" if online else ";online=?0") for host, pool, online in names),
              })
        stale = actual - expected
        if stale:
          await self.tailscale.exec_command(
              worker.hostname, "rm -f " + " ".join(shlex.quote(f"{dynamic}/{name}") for name in sorted(stale)))
        await self.write_worker_revision(worker.hostname, "discovery", stamp)
      except Exception as error:
        failures.append(f"{worker.hostname} cleanup/discovery: {error}")

    if zone is not None:
      orphaned = {record["comment"]: {} for record in zone
                  if (domain_id := domain_artifact_owner("dns", record.get("comment") or "")) is not None
                  and domain_id not in domains}
      discovery = {record["comment"]: {} for record in zone if (record.get("comment") or "").startswith("sage-discovery-")}
      for worker in workers:
        discovery[f"sage-discovery-{worker.hostname}"] = {f"discovery.int.{self.cloudflare.domain}": worker.ip}
      try:
        await run_in_executor_with_context(self.cloudflare.sync_dns_records, {
            **orphaned, "sage-tunnel": {f"discovery.{self.cloudflare.domain}": f"{self.cloudflare.tunnel_id}.cfargotunnel.com"},
        }, zone=zone, type="CNAME", proxied=True)
        await run_in_executor_with_context(self.cloudflare.sync_dns_records, discovery, zone=zone, type="A", proxied=False)
      except Exception as error:
        failures.append(f"Discovery DNS/cleanup: {error}")
    if failures:
      message = "Domain reconciliation failed: " + "; ".join(failures)
      self.notify(message, "error", "/domains")
      raise RuntimeError(message)
    logger.info(f"Domains reconciled; discovery synced: {len(services)} service(s).")
