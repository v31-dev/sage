"""A container's status, decided from the facts its owners hold: the record on its worker, what the worker reports
of its runtime and the operation running on it."""
from dataclasses import dataclass, replace

from services.containers.record import Authority, TargetRead

HEADLINES = ("active", "inactive", "deploying", "stopping", "backup", "restoring", "error", "checking")
CONFIG = ("current", "dirty", "not_deployed", "unknown")
KNOWLEDGE = ("unobserved", "known", "stale", "unreachable")
PROGRESS = {"deploy": "deploying", "stop": "stopping", "delete": "stopping", "backup_stopped": "backup",
            "backup_preserve": "backup", "restore_stopped": "restoring"}
RUNNING = ("running", "paused", "restarting")


@dataclass(frozen=True)
class RuntimeReading:
  """Docker's state of the container (`absent` when the worker has none), the authority and deployment its labels
  name, its health and how it last exited."""
  state: str
  deployment_id: str | None = None
  health: str | None = None
  exit_code: int | None = None
  oom_killed: bool = False
  authority: Authority | None = None


@dataclass(frozen=True)
class Runtime:
  """How much is known of the runtime now, with the last reading, which is current only when `knowledge` is known."""
  knowledge: str
  reading: RuntimeReading | None = None

  def __post_init__(self):
    if self.knowledge == "known" and self.reading is None:
      raise ValueError("A known runtime has a reading.")


@dataclass(frozen=True)
class ContainerFacts:
  expected: Authority
  target: TargetRead
  runtime: Runtime
  live: str | None = None
  desired_hash: str | None = None


@dataclass(frozen=True)
class ContainerView:
  headline: str
  config: str
  reason: str | None = None
  detail: str | None = None


def lifecycle_refusal(target: TargetRead, expected: Authority, contract: str) -> str | None:
  """Why a lifecycle operation may not begin, from the read of the container's record just taken: its worker cannot
  be asked, or the record is another Sage's; and for all but a Delete, which never waits for old work, the record
  cannot be read or its operation never settled. A record of this Sage from an earlier state generation whose
  operation settled does not stop it."""
  if target.kind in ("unobserved", "unreachable"):
    return "unreachable"
  record = target.record
  if record is not None and record.authority.instance_id != expected.instance_id:
    return "authority"
  if contract == "delete":
    return None
  if target.kind == "invalid":
    return "invalid_record"
  if record is not None and record.operation is not None and record.operation.phase != "settled":
    return "interrupted"
  return None


@dataclass(frozen=True)
class Correction:
  """What reconciliation does about a container: `read` its record, `recover` from an operation that never settled,
  or `reset` a runtime that contradicts its record, with the cause."""
  kind: str
  cause: str | None = None


def correction(target: TargetRead, runtime: Runtime, live: str | None, expected: Authority,
               reporting: bool) -> Correction | None:
  """What to do about a container no operation is running on, from its record as last read, its runtime and whether
  its worker reports. A record never read is read; one whose read failed is read again while the worker reports, and
  only then is anything else done. Only a current runtime counts, and only one labelled as this Sage's; a reset only
  stops, and another Sage's record or runtime is left alone."""
  if live is not None:
    return None
  if target.kind == "unobserved":
    return Correction("read")
  if not reporting:
    return None
  if target.kind == "unreachable":
    return Correction("read")
  if target.kind == "invalid":
    return Correction("recover", "unreadable")
  record = target.record
  reading = runtime.reading if runtime.knowledge == "known" else None
  running = reading is not None and reading.state in RUNNING
  ours = reading is not None and reading.authority is not None and reading.authority.instance_id == expected.instance_id
  if record is None:
    return Correction("reset", "no_record") if running and ours else None
  if record.authority.instance_id != expected.instance_id:
    return None
  operation = record.operation
  if operation is not None and operation.phase != "settled":
    # TODO(36 step 4G): an interrupted automatic backup restores its recorded runtime.
    return None if operation.contract == "backup_preserve" else Correction("recover", "interrupted")
  if record.authority != expected:
    return Correction("reset", "restored")
  if not (running and ours):
    return None
  if reading.authority != expected:
    return Correction("reset", "restored")
  settled = record.settled
  if settled is None or settled.runtime_goal == "stopped":
    return Correction("reset", "unexpected_running")
  if record.applied is None or reading.deployment_id != record.applied.deployment_id:
    return Correction("reset", "unexpected_deployment")
  return None


def decide_container(facts: ContainerFacts) -> ContainerView:
  """The headline and the configuration comparison. The headline is decided in precedence order: a record or a
  current runtime of another authority (this Sage's before a restore, or another Sage's), a live operation's progress, a known failure, too little evidence, then the committed goal against the
  current runtime."""
  record, runtime = facts.target.record, facts.runtime
  foreign = record is not None and record.authority != facts.expected
  others = [authority for authority in (record.authority if record is not None else None,
                                        runtime.reading.authority if runtime.knowledge == "known" else None)
            if authority not in (None, facts.expected)]
  if record is None or foreign:
    config = "not_deployed" if facts.target.kind == "absent" else "unknown"
  elif record.applied is None:
    config = "not_deployed"
  elif facts.desired_hash is None:
    config = "unknown"
  else:
    config = "current" if record.applied.config_hash == facts.desired_hash else "dirty"

  def view(headline, reason=None, detail=None):
    return ContainerView(headline, config, reason, detail)

  if others:
    # This Sage's own record or runtime from before a restore, or another Sage's.
    restored = all((authority.instance_id, authority.container_id) == (facts.expected.instance_id, facts.expected.container_id)
                   for authority in others)
    return view("error", "restored" if restored else "authority")
  if facts.live in PROGRESS:
    return view(PROGRESS[facts.live])
  if facts.target.kind == "invalid":
    return view("error", "invalid_record")
  operation = record.operation if record else None
  if operation is not None and operation.phase != "settled" and facts.live is None:
    return view("checking", "backup_interrupted") if operation.contract == "backup_preserve" else view("error", "interrupted")
  settled = record.settled if record else None
  if settled is not None and settled.result == "failed":
    return view("error", "failed", settled.reason)
  if runtime.knowledge == "unreachable":
    return view("error", "unreachable")
  if (record is None and facts.target.kind != "absent") or runtime.knowledge != "known":
    return view("checking")
  reading = runtime.reading
  if settled is None or settled.runtime_goal == "stopped":
    return view("error", "unexpected_running", reading.state) if reading.state in RUNNING else view("inactive")
  if reading.state != "running":
    return view("error", "unexpected_exit", reading.state)
  if record.applied is None or reading.deployment_id != record.applied.deployment_id:
    return view("error", "unexpected_deployment", reading.deployment_id)
  return view("active")


def routable(facts: ContainerFacts) -> bool | None:
  """Whether the container can be routed to: active by its record and runtime, whatever operation runs on it, since an
  operation records that it began before it stops the container. None while that is not known: too little evidence,
  or its worker can't be reached."""
  view = decide_container(replace(facts, live=None))
  return None if view.headline == "checking" or view.reason == "unreachable" else view.headline == "active"
