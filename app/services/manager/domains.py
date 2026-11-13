"""Each Domain's apply, which brings its routing file and DNS records in line with its configuration, and the pass
over every Domain's artifacts, which sees what they are, removes those left behind and requests the applies they call
for; the requests that queue them, and each Domain's state, with what tells its pages that it changed."""
import asyncio
import logging
from datetime import datetime

import config
from services.cloudflare import Cloudflare, dns_record_matches
from services.containers.state import container_reference
from services.db.models import Application, Container, Domain, Project, Worker
from services.domains.config import (DomainConfig, domain_artifact_name, domain_artifact_owner, domain_scope, host_scope, parse_route_listing,
                                     route_scope, routes_command)
from services.domains.decisions import ApplyOutcome, RouteObservation, route_goal
from services.domains.state import Domains, DomainState, domain_payload, domain_state
from services.domains.updates import domain_changed
from services.notification import Notifications
from services.tailscale import Tailscale
from services.updates import Fact
from services.workers.state import Workers
from utils.common import get_env
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

# What a notice calls each part of a Domain's apply.
APPLY_PARTS = {"route": "its route", "dns": "its DNS records"}


def capture(domain: Domain) -> DomainConfig:
  return DomainConfig.capture(domain, get_env("DOMAIN"))


class DomainsMixin:
  def domain_states(self, domains) -> dict[str, DomainState]:
    """The state of each of `domains` now, by id, their containers' and workers' states worked out once."""
    configs = {domain.id: capture(domain) for domain in domains}
    containers = self.container_states(Container.select().where(
        Container.id.in_({snapshot.container_id for snapshot in configs.values()} - {None})))
    workers = self.worker_states(list({snapshot.worker for snapshot in configs.values()} - {None}))
    applying = self._applying_domains()
    return {domain_id: domain_state(snapshot, containers[snapshot.container_id].routable if snapshot.container_id else None,
                                    workers[snapshot.worker].view.ready if snapshot.worker else None, applying.get(domain_id))
            for domain_id, snapshot in configs.items()}

  def domain_payloads(self, domains) -> list[dict]:
    """The payload of each of `domains`, in their order."""
    domains = list(domains)
    states = self.domain_states(domains)
    return [domain_payload(domain, states[domain.id], get_env("DOMAIN")) for domain in domains]

  def domains_page(self) -> dict:
    """Every domain's payload, by type and name, and every container one can be linked to, by application and worker,
    with its state."""
    containers = list(Container.select(Container, Application, Project, Worker).join(Application).join(Project)
                      .switch(Container).join(Worker).order_by(Project.name, Application.name, Container.worker))
    states = self.container_states(containers)
    return {"domains": self.domain_payloads(Domain.select().order_by(Domain.type, Domain.name)),
            "containers": [container_reference(container, states[container.id]) for container in containers]}

  def domains_queue_facts(self) -> list[Fact]:
    """The applies queued or running: a domain whose apply is queued, starts or ends is shown again."""
    previous = frozenset()

    def changed(current):
      nonlocal previous
      affected = current ^ previous
      previous = current
      for domain in Domain.select().where(Domain.id.in_(affected)):
        domain_changed(domain)

    return [Fact(lambda: frozenset(self._applying_domains()), on_change=changed)]

  def request_domain_applies(self, domain_ids):
    """Queues the apply of each of the domains as it is configured now, holding the domain, its full name, and its
    route on the worker it goes to. A running apply is not interrupted: the latest request waits for it."""
    for domain in Domain.select().where(Domain.id.in_(list(domain_ids))):
      snapshot = capture(domain)
      scopes = {domain_scope(domain.id)}
      if snapshot.host is not None:
        scopes.add(host_scope(snapshot.host))
      if snapshot.worker is not None:
        scopes.add(route_scope(snapshot.worker, domain.id))
      self.add_task(task=self.apply_domain, key=("apply_domain", domain.id), scopes=scopes, executor="app",
                    params={"domain_id": domain.id, "worker": snapshot.worker, "host": snapshot.host},
                    labels={"Domain": snapshot.host or domain.name}, on_conflict=OnConflict.REPLACE, quiet=True)

  async def apply_domain(self, domain_id: str, worker: str | None, host: str | None):
    """Writes the domain's routing file on its worker while its container can be routed to and removes it once it
    can't or the domain is unlinked, leaving it alone while that is not known or the worker isn't reachable and set up;
    then makes its DNS records what its configuration gives. Holds the outcome for the fingerprint it applied, a
    success only when the route was applied too. An edit during the apply queues another; discovery, whose
    availability follows the outcome, is published again."""
    domain = Domain.get_or_none(Domain.id == domain_id)
    if domain is None:
      Domains().forget(domain_id)
      logger.info(f"Domain {domain_id} was deleted before its apply; nothing to do.")
      return
    snapshot = capture(domain)
    if (snapshot.worker, snapshot.host) != (worker, host):
      self.request_domain_applies([domain_id])
      return
    fingerprint = snapshot.content_hash
    eligible = None
    if snapshot.container_id is not None:
      eligible = self.container_states([domain.container])[snapshot.container_id].routable
    goal = route_goal(snapshot.container_id is not None, eligible)
    routed = worker is None or (goal is not None and Tailscale().ssh(worker) == "reachable"
                                and Workers().setup(worker) == "succeeded")
    phase, failure = "route", None
    try:
      if worker is not None and routed:
        path = f"{config.WORKER_HOME_DIR}/traefik/dynamic/{snapshot.filename}"
        if goal == "present":
          await Tailscale().sync_file(worker, config.TEMPLATES_DIR / "worker/file", path, {"CONTENT": snapshot.route()})
          Domains().record_route(domain, worker, RouteObservation("present", fingerprint))
        else:
          await Tailscale().exec_command(worker, f"rm -f {path}", timeout=30)
          Domains().record_route(domain, worker, RouteObservation("absent"))
      phase = "dns"
      cloudflare = Cloudflare()
      await run_in_executor_with_context(cloudflare.connect)
      tunnel = None
      if snapshot.type == "public" and snapshot.container_id is not None:
        tunnel = await run_in_executor_with_context(cloudflare.find_tunnel, cloudflare.worker_tunnel_name(worker))
      await run_in_executor_with_context(cloudflare.sync_dns_records, {snapshot.dns_comment: snapshot.dns_records(tunnel)},
                                         type="CNAME", proxied=True)
      Domains().record_dns(domain, fingerprint, True)
    except Exception as error:
      failure = str(error) or type(error).__name__
      Domains().record_outcome(domain, ApplyOutcome(fingerprint, False, phase, failure))
    else:
      if routed:
        Domains().record_outcome(domain, ApplyOutcome(fingerprint, True))

    current = Domain.get_or_none(Domain.id == domain_id)
    if current is None:
      Domains().forget(domain_id)
    elif capture(current).content_hash != fingerprint:
      self.request_domain_applies([domain_id])
    self.request_discovery()
    if failure is not None:
      Notifications().notify(f"Domain {snapshot.host or domain.name}: {APPLY_PARTS[phase]} could not be applied: {failure}", "error",
                             "/domains")
      raise TaskFailed()
    if not routed:
      logger.info(f"Domain {snapshot.host or domain.name}: DNS applied; its route waits for "
                  + ("its container's state to be known." if goal is None else f"worker {worker}."))

  def domain_edited(self, domain_id: str):
    """After the domain's edit (its command): it is applied as it is configured now, and the pass withdraws what the edit
    left behind, such as its route on the worker it was on."""
    self.request_domain_applies([domain_id])
    self.request_domain_reconciliation()

  def request_domain_reconciliation(self):
    """Queues the pass over every Domain's artifacts. A request while one runs gets a pass of its own after it, so a
    change that pass did not see is not missed."""
    self.add_task(task=self.reconcile_domains, key=("reconcile_domains",), scopes={domain_scope()}, executor="app",
                  on_conflict=OnConflict.REPLACE, quiet=True)

  async def reconcile_domains(self):
    """Lists the routing files on every worker reachable and set up, the zone and the tunnels; removes the routing files
    of Domains gone or bound to another worker, and the DNS records of Domains gone; holds what it saw of each Domain's
    route and DNS; then requests the apply of each Domain in error, drifted or not applied, and discovery's
    publication. Holding every Domain, it never crosses an apply; it lists before it reads the rows, so a Domain
    created meanwhile is never taken for one gone."""
    hostnames = [worker.hostname for worker in Worker.select(Worker.hostname)
                 if Tailscale().ssh(worker.hostname) == "reachable" and Workers().setup(worker.hostname) == "succeeded"]
    cloudflare, failures = Cloudflare(), []
    zone, tunnels, *listings = await asyncio.gather(
        run_in_executor_with_context(cloudflare.get_dns_records), run_in_executor_with_context(cloudflare.tunnels),
        *(Tailscale().exec_command(hostname, routes_command(f"{config.WORKER_HOME_DIR}/traefik/dynamic"), timeout=30)
          for hostname in hostnames), return_exceptions=True)
    for name, listed in (("the DNS zone", zone), ("the tunnels", tunnels)):
      if isinstance(listed, Exception):
        failures.append(f"{name} could not be listed: {listed}")
    routes = {}
    for hostname, listing in zip(hostnames, listings):
      if isinstance(listing, Exception):
        failures.append(f"the routing files on {hostname} could not be listed: {listing}")
      else:
        routes[hostname] = parse_route_listing(listing[1])

    domains = {domain.id: domain for domain in Domain.select()}
    configs = {domain_id: capture(domain) for domain_id, domain in domains.items()}
    removals = await asyncio.gather(*(self.remove_obsolete_routes(hostname, listed, configs)
                                      for hostname, listed in routes.items()), return_exceptions=True)
    failures += [f"the routing files on {hostname} could not be removed: {error}"
                 for hostname, error in zip(routes, removals) if isinstance(error, Exception)]
    if not isinstance(zone, Exception):
      orphaned = {record["comment"] for record in zone
                  if (owner := domain_artifact_owner("dns", record.get("comment") or "")) and owner not in configs}
      try:
        if orphaned:
          await run_in_executor_with_context(cloudflare.sync_dns_records, dict.fromkeys(orphaned, {}), zone=zone)
      except Exception as error:
        failures.append(f"the DNS records of deleted domains could not be removed: {error}")

    for domain_id, snapshot in configs.items():
      domain = domains[domain_id]
      if snapshot.worker is not None:
        listed = routes.get(snapshot.worker)
        Domains().record_route(domain, snapshot.worker, RouteObservation("unreachable") if listed is None else
                               RouteObservation("present", listed[domain_id]) if domain_id in listed else RouteObservation("absent"))
      if not isinstance(zone, Exception) and not isinstance(tunnels, Exception):
        records = [record for record in zone if record.get("comment") == snapshot.dns_comment]
        try:
          wanted = snapshot.dns_records(tunnels.get(cloudflare.worker_tunnel_name(snapshot.worker)))
        except ValueError:
          conforms = False
        else:
          conforms = len(records) == len(wanted) and all(
              record["name"] in wanted and dns_record_matches(record, {"content": wanted[record["name"]], "type": "CNAME", "proxied": True})
              for record in records)
        Domains().record_dns(domain, snapshot.content_hash, conforms)
    Domains().retain(configs.keys())
    self.request_domain_applies(domain_id for domain_id, state in self.domain_states(domains.values()).items()
                                if state.view.status == "error" or state.view.reason in ("drifted", "not_applied"))
    self.request_discovery()
    if failures:
      Notifications().notify("The domains could not all be checked: " + "; ".join(failures), "error", "/domains")
      raise TaskFailed()
    logger.info(f"Domains reconciled on {len(routes)} worker(s).")

  async def remove_obsolete_routes(self, hostname: str, listed, configs: dict[str, DomainConfig]):
    """Removes, in one command, the routing files on the worker among `listed` (Domain ids) whose Domain is gone or
    goes to another worker; never for its container's state, which is its apply's."""
    obsolete = sorted(domain_id for domain_id in listed if domain_id not in configs or configs[domain_id].worker != hostname)
    if obsolete:
      await Tailscale().exec_command(hostname, "rm -f " + " ".join(
          f"{config.WORKER_HOME_DIR}/traefik/dynamic/{domain_artifact_name('file', domain_id)}" for domain_id in obsolete), timeout=30)
      logger.info(f"Removed the routing files of {len(obsolete)} domain(s) gone or moved from {hostname}.")

  def _applying_domains(self) -> dict[str, datetime]:
    """The domains with an apply queued or running, each with when the oldest of them was queued."""
    snapshot = self.task_snapshot()
    applying = {}
    for task in (*snapshot["running"], *snapshot["queued"]):
      if task["name"] == "apply_domain":
        domain_id = task["key"][1]
        applying[domain_id] = min(applying.get(domain_id, task["queued_at"]), task["queued_at"])
    return applying
