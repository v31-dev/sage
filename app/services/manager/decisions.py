"""The decisions of worker setup and stack repair, as plain functions of the state they are given."""

import config
from services.worker_reports import Snapshot


def worker_owner(revisions: dict) -> tuple[str, str] | None:
  """The Sage that a worker belongs to, as (instance id, domain) from its owner stamp, or None when it
  has none."""
  instance_id, _, domain = revisions.get("owner", "").partition(" ")
  return (instance_id, domain) if instance_id else None


def worker_claim_refusal(revisions: dict, instance_id: str) -> str | None:
  """Why this Sage may not set a worker up: its owner stamp names another instance. None when it is
  unowned or this Sage's."""
  owner = worker_owner(revisions)
  if owner and owner[0] != instance_id:
    return f"it is managed by another Sage ({owner[1] or 'unknown domain'}); remove it from there first"
  return None


def worker_stack_problem(snapshot: Snapshot | None, silent_for: float) -> str | None:
  """What a worker's report says is wrong with its stack, None when nothing is. `snapshot` is the
  current one, or None when the worker has no current report, `silent_for` the seconds since it
  last reported."""
  if snapshot is None:
    return "not reporting" if silent_for > config.WORKER_STACK_SILENCE_SECONDS else None
  down = [name for name in config.WORKER_STACK_SERVICES
          if name not in snapshot.containers or snapshot.containers[name].status != "running"]
  if down:
    return f"{', '.join(down)} not running"
  if snapshot.traefik_reachable is False:
    return "Traefik is not answering"
  return None
