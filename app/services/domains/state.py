"""Each Domain's state as its pages show it, worked out from the facts its owners hold, and what Sage knows of each
Domain's artifacts since the Manager started."""
from dataclasses import asdict, dataclass

from services.base import Base
from services.db.models import Domain
from services.domains.config import DomainConfig, address
from services.domains.decisions import ApplyOutcome, DomainFacts, DomainView, RouteObservation, decide_domain
from services.domains.updates import domain_changed


class Domains(Base):
  """By Domain id: how its last apply ended, for the fingerprint it applied; what was last seen of its routing file, on
  the worker it was looked for on; and whether its DNS records conformed, for the fingerprint they were compared
  with. Nothing until an apply or a scan, since none of it is stored."""

  def __init__(self):
    super().__init__()
    self._outcomes: dict[str, ApplyOutcome] = {}
    self._routes: dict[str, tuple[str, RouteObservation]] = {}
    self._dns: dict[str, tuple[str, bool]] = {}

  def outcome(self, domain_id: str) -> ApplyOutcome | None:
    with self.lock:
      return self._outcomes.get(domain_id)

  def route(self, domain_id: str, worker: str | None) -> RouteObservation:
    """What was last seen of its routing file on `worker`; unobserved when it was last looked for on another."""
    with self.lock:
      seen = self._routes.get(domain_id)
    return seen[1] if seen is not None and seen[0] == worker else RouteObservation("unobserved")

  def dns_conforms(self, domain_id: str, fingerprint: str) -> bool | None:
    with self.lock:
      seen = self._dns.get(domain_id)
    return seen[1] if seen is not None and seen[0] == fingerprint else None

  def record_outcome(self, domain: Domain, outcome: ApplyOutcome):
    self._hold(domain, self._outcomes, outcome)

  def record_route(self, domain: Domain, worker: str, observation: RouteObservation):
    self._hold(domain, self._routes, (worker, observation))

  def record_dns(self, domain: Domain, fingerprint: str, conforms: bool):
    self._hold(domain, self._dns, (fingerprint, conforms))

  def forget(self, domain_id: str):
    """Drops what is held of the domain once its row is gone."""
    with self.lock:
      for held in (self._outcomes, self._routes, self._dns):
        held.pop(domain_id, None)

  def clear(self):
    """Drops what is held of every domain, as when the Manager starts: after a platform restore."""
    self.retain(())

  def retain(self, domain_ids):
    """Drops what is held of every domain but `domain_ids`, the rows there are."""
    kept = set(domain_ids)
    with self.lock:
      for held in (self._outcomes, self._routes, self._dns):
        for domain_id in held.keys() - kept:
          del held[domain_id]

  def _hold(self, domain: Domain, held: dict, value):
    with self.lock:
      changed = held.get(domain.id) != value
      held[domain.id] = value
    if changed:
      domain_changed(domain)


@dataclass(frozen=True)
class DomainState:
  """The decided view with the configuration and the facts it was decided from."""
  view: DomainView
  config: DomainConfig
  facts: DomainFacts


def domain_state(config: DomainConfig, eligible: bool | None, worker_ready: bool | None, syncing: bool) -> DomainState:
  """The domain's state now, given its configuration, whether its container can be routed to, whether its worker is
  ready and whether an apply of it is queued or running."""
  fingerprint = config.content_hash
  facts = DomainFacts(fingerprint, config.container_id is not None, eligible, worker_ready,
                      Domains().route(config.domain_id, config.worker), Domains().dns_conforms(config.domain_id, fingerprint),
                      Domains().outcome(config.domain_id), syncing)
  return DomainState(decide_domain(facts), config, facts)


def domain_payload(domain: Domain, state: DomainState, base_domain: str) -> dict:
  """The domain as its pages show it: its configuration, its full name and how it is reached, the worker its route goes
  to, the container it is linked to (none once that is deleted) with its application, and its decided state."""
  container = domain.container
  application = container.application if container else None
  return {
      "id": domain.id,
      "name": domain.name,
      "type": domain.type,
      "port": domain.port,
      "service": domain.service,
      "pool": domain.pool,
      "cors_origins": domain.cors_origins,
      "host": state.config.host,
      "address": address(domain, base_domain),
      "worker": state.config.worker,
      "container": container and {
          "id": container.id,
          "worker": container.worker_id,
          "application": {
              "name": application.name,
              "label": application.label,
              "project": {"name": application.project.name, "label": application.project.label},
          },
      },
      "view": asdict(state.view),
      "created_at": domain.created_at,
      "updated_at": domain.updated_at,
  }
