"""A container's status, decided from the facts its owners hold: the record on its worker, what the worker reports
of its runtime and the operation running on it."""
from dataclasses import dataclass

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

  @property
  def routable(self) -> bool:
    return self.headline == "active"


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
