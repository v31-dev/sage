"""A Domain's apply status, decided from what its owners hold: its desired fingerprints, whether its container can be
routed, what the scan found on each worker it has a file on and in DNS, the outcome of its last apply and whether a
sync is under way."""
from dataclasses import dataclass
from datetime import datetime

STATUSES = ("syncing", "pending", "synced", "error")
ROUTES = ("unobserved", "present", "absent", "unreachable")


@dataclass(frozen=True)
class RouteObservation:
  """What the scan found of the domain's routing file: present with the fingerprint its header names (None when the
  header is not its own), absent, or not known."""
  kind: str
  fingerprint: str | None = None


@dataclass(frozen=True)
class ApplyOutcome:
  """How the last apply of `fingerprint` ended; `phase` and `error` say where and why one failed."""
  fingerprint: str
  succeeded: bool
  phase: str | None = None
  error: str | None = None


@dataclass(frozen=True)
class WorkerRoute:
  """Its file on one worker: the fingerprint it should carry (None while that isn't known), what the scan found and
  whether the worker is ready."""
  worker: str
  expected: str | None
  observed: RouteObservation
  ready: bool | None


@dataclass(frozen=True)
class DomainFacts:
  """`fingerprint` is its configuration's; `eligible` is its container's `routable`, None while that is not known;
  `worker_ready` is its own worker's; `routes` are its files on its own worker and each caller's; `dns_conforms` is
  None when the zone was not compared; `syncing_since` is when the oldest of its applies queued or running was queued,
  None without one."""
  fingerprint: str
  eligible: bool | None
  worker_ready: bool | None
  routes: tuple[WorkerRoute, ...]
  dns_conforms: bool | None
  outcome: ApplyOutcome | None = None
  syncing_since: datetime | None = None


@dataclass(frozen=True)
class DomainView:
  status: str
  conforms: bool | None
  reason: str | None = None
  error: str | None = None


def route_goal(eligible: bool | None) -> str | None:
  """Whether the routing file should be on the worker: present while its container can be routed, absent once it
  cannot, None while that is not known, which removes nothing."""
  if eligible is False:
    return "absent"
  return "present" if eligible else None


def decide_domain(facts: DomainFacts) -> DomainView:
  """Whether its artifacts conform, and its status: syncing while a sync is under way; synced when they conform, or
  when the last apply of this fingerprint succeeded and nothing seen since contradicts it; error when that apply
  failed; otherwise pending, with why. A file whose expected fingerprint isn't known counts as not known, never as
  drifted."""
  goal = route_goal(facts.eligible)

  def conforms(route: WorkerRoute) -> bool | None:
    if goal is None or route.observed.kind in ("unobserved", "unreachable"):
      return None
    if goal == "absent":
      return route.observed.kind == "absent"
    if route.expected is None:
      return None
    return route.observed.kind == "present" and route.observed.fingerprint == route.expected

  parts = (*(conforms(route) for route in facts.routes), facts.dns_conforms)
  conforms = False if False in parts else None if None in parts else True

  outcome = facts.outcome if facts.outcome is not None and facts.outcome.fingerprint == facts.fingerprint else None
  if facts.syncing_since is not None:
    return DomainView("syncing", conforms)
  if conforms is True or (outcome is not None and outcome.succeeded and conforms is None):
    return DomainView("synced", conforms)
  if outcome is not None and not outcome.succeeded:
    return DomainView("error", conforms, outcome.phase, outcome.error)
  if conforms is False:
    return DomainView("pending", conforms, "drifted")
  if facts.worker_ready is False or any(route.ready is False for route in facts.routes):
    return DomainView("pending", conforms, "worker_not_ready")
  if goal is None:
    return DomainView("pending", conforms, "eligibility_unknown")
  return DomainView("pending", conforms, "not_applied")
