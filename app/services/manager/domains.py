"""Each Domain's apply, which brings its routing file and DNS records in line with its configuration, the requests
that queue it, and each Domain's state, with what tells its pages that it changed."""
import logging

import config
from services.cloudflare import Cloudflare
from services.db.models import Container, Domain
from services.domains.config import DomainConfig
from services.domains.decisions import ApplyOutcome, RouteObservation, route_goal
from services.domains.state import Domains, DomainState, domain_state
from services.domains.updates import domain_changed
from services.tailscale import Tailscale
from services.updates import Fact
from services.workers.state import Workers
from utils.common import get_env
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed
from utils.queue import OnConflict

logger = logging.getLogger(__name__)


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
                                    workers[snapshot.worker].view.ready if snapshot.worker else None, domain_id in applying)
            for domain_id, snapshot in configs.items()}

  def domains_queue_facts(self) -> list[Fact]:
    """The applies queued or running: a domain whose apply is queued, starts or ends is shown again."""
    previous = frozenset()

    def changed(current):
      nonlocal previous
      affected = current ^ previous
      previous = current
      for domain in Domain.select().where(Domain.id.in_(affected)):
        domain_changed(domain)

    return [Fact(self._applying_domains, on_change=changed)]

  def request_domain_applies(self, domain_ids):
    """Queues the apply of each of the domains as it is configured now, holding the domain, its full name, and its
    route on the worker it goes to. A running apply is not interrupted: the latest request waits for it."""
    for domain in Domain.select().where(Domain.id.in_(list(domain_ids))):
      snapshot = capture(domain)
      scopes = {f"domain:{domain.id}"}
      if snapshot.host is not None:
        scopes.add(f"domain:host:{snapshot.host}")
      if snapshot.worker is not None:
        scopes.add(f"worker:{snapshot.worker}:routing:{domain.id}")
      self.add_task(task=self.apply_domain, key=("apply_domain", domain.id), scopes=scopes, executor="app",
                    params={"domain_id": domain.id, "worker": snapshot.worker, "host": snapshot.host},
                    labels={"Domain": snapshot.host or domain.name}, on_conflict=OnConflict.REPLACE, quiet=True)

  async def apply_domain(self, domain_id: str, worker: str | None, host: str | None):
    """Writes the domain's routing file on its worker while its container can be routed to and removes it once it
    can't or the domain is unlinked, leaving it alone while that is not known or the worker isn't reachable and set up;
    then makes its DNS records what its configuration gives. Holds the outcome for the fingerprint it applied, a
    success only when the route was applied too. An edit during the apply queues another."""
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
    # TODO(36 step 4E): request discovery, whose availability follows the apply.
    if failure is not None:
      logger.error(f"Domain {snapshot.host or domain.name}: the {phase} apply failed: {failure}")
      # TODO(36 step 4H): self.notify(f"Domain {snapshot.host or domain.name} sync failed at {phase}: {failure}", "error", "/domains")
      raise TaskFailed()
    if not routed:
      logger.info(f"Domain {snapshot.host or domain.name}: DNS applied; its route waits for "
                  + ("its container's state to be known." if goal is None else f"worker {worker}."))

  def _applying_domains(self) -> frozenset[str]:
    """The domains with an apply queued or running."""
    snapshot = self.task_snapshot()
    return frozenset(task["params"]["domain_id"] for task in (*snapshot["running"], *snapshot["queued"])
                     if task["name"] == "apply_domain")
