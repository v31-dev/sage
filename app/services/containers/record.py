from dataclasses import dataclass

CONTRACTS = ("deploy", "stop", "delete", "backup_stopped", "backup_preserve", "restore_stopped", "reset")
PHASES = ("executing", "recovering", "settled")
READS = ("unobserved", "present", "absent", "invalid", "unreachable")


@dataclass(frozen=True)
class Authority:
  instance_id: str
  state_generation: str
  container_id: str


@dataclass(frozen=True)
class Applied:
  """The last successful deployment: its operation id and the fingerprint of the inputs it applied."""
  deployment_id: str
  config_hash: str


@dataclass(frozen=True)
class Settled:
  """The verified outcome of the last intentional action: the runtime it left (running or stopped) and whether it
  succeeded."""
  operation_id: str
  runtime_goal: str
  result: str
  reason: str | None = None


@dataclass(frozen=True)
class Operation:
  """The latest admitted operation; `outcome` stays None until it is decided."""
  id: str
  contract: str
  phase: str
  outcome: str | None = None


@dataclass(frozen=True)
class TargetRecord:
  authority: Authority
  applied: Applied | None = None
  settled: Settled | None = None
  operation: Operation | None = None


@dataclass(frozen=True)
class TargetRead:
  """What the last read of the record found, with the last record decoded (kept while a later read could not reach
  the worker)."""
  kind: str
  record: TargetRecord | None = None
