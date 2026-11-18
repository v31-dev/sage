"""A Domain's apply status, decided from what its owners hold: its desired fingerprint, whether its container can be
routed, what the scan found on its worker and in DNS, the outcome of its last apply and whether a sync is under way."""
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
class DomainFacts:
  """`eligible` is its container's `routable`, None while that is not known; `dns_conforms` is None when the zone was
  not compared; `syncing_since` is when the oldest of its applies queued or running was queued, None without one."""
  fingerprint: str
  linked: bool
  eligible: bool | None
  worker_ready: bool | None
  route: RouteObservation
  dns_conforms: bool | None
  outcome: ApplyOutcome | None = None
  syncing_since: datetime | None = None


@dataclass(frozen=True)
class DomainView:
  status: str
  conforms: bool | None
  reason: str | None = None
  error: str | None = None


def route_goal(linked: bool, eligible: bool | None) -> str | None:
  """Whether the routing file should be on the worker: present for a linked domain whose container can be routed,
  absent for an unlinked one or one whose container cannot, None while that is not known, which removes nothing."""
  if not linked or eligible is False:
    return "absent"
  return "present" if eligible else None


def decide_domain(facts: DomainFacts) -> DomainView:
  """Whether its artifacts conform, and its status: syncing while a sync is under way; synced when they conform, or
  when the last apply of this fingerprint succeeded and nothing seen since contradicts it; error when that apply
  failed; otherwise pending, with why."""
  goal, route = route_goal(facts.linked, facts.eligible), facts.route
  if goal is None or route.kind in ("unobserved", "unreachable"):
    route_conforms = None
  elif goal == "present":
    route_conforms = route.kind == "present" and route.fingerprint == facts.fingerprint
  else:
    route_conforms = route.kind == "absent"
  parts = (route_conforms, facts.dns_conforms)
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
  if facts.linked and facts.worker_ready is False:
    return DomainView("pending", conforms, "worker_not_ready")
  if goal is None:
    return DomainView("pending", conforms, "eligibility_unknown")
  return DomainView("pending", conforms, "not_applied")
