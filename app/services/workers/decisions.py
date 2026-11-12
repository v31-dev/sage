"""A worker's status, decided from the facts its owners hold: whether its SSH server answers, how its setup went, what
its newest report says and which of its tasks are queued or running."""
from dataclasses import dataclass
from datetime import datetime

import config
from services.worker_reports import Received, is_current

STATUSES = ("removing", "adding", "rebooting", "checking", "offline", "not ready", "online")
SSH = ("unobserved", "reachable", "unreachable", "unknown")
SETUPS = (None, "succeeded", "failed", "refused")
TASKS = {"remove_worker": "removing", "add_worker": "adding", "reboot_worker": "rebooting"}


@dataclass(frozen=True)
class WorkerReport:
  """How much is known of the worker from its reports, and what the newest says of its stack and of Traefik: the
  services of its stack that do not run, each service's status (`missing` when it is not listed), and how many
  containers it lists."""
  knowledge: str
  traefik_reachable: bool | None = None
  stack_down: tuple[str, ...] = ()
  stack: tuple[tuple[str, str], ...] = ()
  containers: int | None = None


@dataclass(frozen=True)
class WorkerFacts:
  """`ssh` is unknown while the Manager itself is cut off from the tailnet; `setup` is None until a setup has run
  since the Manager started, and refused when another Sage owns the worker."""
  ssh: str
  setup: str | None
  report: WorkerReport
  tasks: frozenset[str] = frozenset()
  seconds_since_start: float = 0


@dataclass(frozen=True)
class WorkerView:
  status: str
  reason: str | None = None
  detail: str | None = None

  @property
  def ready(self) -> bool | None:
    """Whether its containers can be reached; None while that is not known yet."""
    return None if self.status == "checking" else self.status == "online"


def report_of(received: Received | None, now: datetime) -> WorkerReport:
  if received is None:
    return WorkerReport("unobserved")
  snapshot = received.snapshot
  stack = tuple((name, snapshot.containers[name].status if name in snapshot.containers else "missing")
                for name in config.WORKER_STACK_SERVICES)
  return WorkerReport("known" if is_current(received.arrived_at, now) else "stale", snapshot.traefik_reachable,
                      tuple(name for name, status in stack if status != "running"), stack, len(snapshot.containers))


def decide_worker(facts: WorkerFacts) -> WorkerView:
  """The status, in precedence order: a task under way, what SSH says, how its setup went, then what its reports
  say. A worker that has not reported yet is checking during REPORT_STARTUP_GRACE_SECONDS after the Manager starts and
  not ready after it: the grace never makes it ready."""
  task = next((TASKS[name] for name in TASKS if name in facts.tasks), None)
  if task is not None:
    return WorkerView(task)
  if facts.ssh == "unreachable":
    return WorkerView("offline", "ssh_unreachable")
  if facts.ssh == "unknown":
    return WorkerView("checking", "manager_offline")
  if facts.ssh == "unobserved" or facts.setup is None:
    return WorkerView("checking", "unobserved")
  if facts.setup == "refused":
    return WorkerView("not ready", "owned_elsewhere")
  if facts.setup == "failed":
    return WorkerView("not ready", "setup_failed")
  report = facts.report
  if report.knowledge == "unobserved":
    if facts.seconds_since_start < config.REPORT_STARTUP_GRACE_SECONDS:
      return WorkerView("checking", "unobserved")
    return WorkerView("not ready", "not_reporting")
  if report.knowledge == "stale":
    return WorkerView("not ready", "not_reporting")
  if report.stack_down:
    return WorkerView("not ready", "stack_down", ", ".join(report.stack_down))
  if report.traefik_reachable is not True:
    return WorkerView("not ready", "traefik_down" if report.traefik_reachable is False else "traefik_unchecked")
  return WorkerView("online")


def discovery_member(ssh: str, setup: str | None, address: str | None, seconds_since_start: float) -> bool | None:
  """Whether `discovery.int.<domain>` points at the worker: yes while it is reachable, set up since the Manager started
  and has an address; no once its SSH server is unreachable or its setup failed or was refused. None while that is not
  known yet: while the Manager is cut off from the tailnet, and otherwise (not observed, or not set up since the start)
  for UNKNOWN_HOLD_SECONDS after the Manager started, then no."""
  if ssh == "unreachable" or setup in ("failed", "refused"):
    return False
  if ssh == "reachable" and setup == "succeeded" and address:
    return True
  return None if ssh == "unknown" or seconds_since_start < config.UNKNOWN_HOLD_SECONDS else False


def reboot_outcome(baseline_boot_id: str, boot_id: str | None, ready: bool | None) -> str:
  """What a requested reboot has achieved: verified once the worker runs a new boot and is ready; old_boot while it
  still runs the one from before the request; not_ready on a new boot that is not ready; unknown while the boot
  could not be read."""
  if boot_id is None:
    return "unknown"
  if boot_id == baseline_boot_id:
    return "old_boot"
  return "verified" if ready is True else "not_ready"


def worker_owner(revisions: dict) -> tuple[str, str] | None:
  """The Sage the worker belongs to, (instance id, domain) from its owner stamp; None when it has none."""
  instance_id, _, domain = revisions.get("owner", "").partition(" ")
  return (instance_id, domain) if instance_id else None


def worker_claim_refusal(revisions: dict, instance_id: str) -> str | None:
  """Why this Sage may not set the worker up: its owner stamp names another instance. None when it is unowned or this
  Sage's."""
  owner = worker_owner(revisions)
  if owner and owner[0] != instance_id:
    return f"it is managed by another Sage ({owner[1] or 'unknown domain'}); remove it from there first"
  return None
