"""The record Sage keeps on a worker for each container it placed there: who owns it, what was last applied, the
verified outcome of the last intentional action and the latest operation. Only the Manager writes it."""
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from utils.common import OPAQUE_ID

FORMAT_VERSION = 1
CONTRACTS = ("deploy", "stop", "delete", "backup_stopped", "backup_preserve", "restore_stopped", "reset")
PHASES = ("executing", "recovering", "settled")
READS = ("unobserved", "present", "absent", "invalid", "unreachable")
GOALS = ("running", "stopped")
RESULTS = ("succeeded", "failed")
OUTCOMES = ("succeeded", "failed", "interrupted")
RECOVERIES = ("unresolved", "verified")


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
  committed_at: datetime


@dataclass(frozen=True)
class Settled:
  """The verified outcome of the last intentional action: the runtime it left and whether it succeeded."""
  operation_id: str
  runtime_goal: str
  result: str
  committed_at: datetime
  reason: str | None = None


@dataclass(frozen=True)
class Baseline:
  """What an automatic backup found before quiescing, to restore after it: the settled operation, whether the
  container ran, the deployment it ran and Docker's ids of its runtime."""
  settled_operation_id: str | None
  runtime: str
  deployment_id: str | None
  docker_ids: tuple[str, ...]


@dataclass(frozen=True)
class Recovery:
  result: str
  reason: str | None = None


@dataclass(frozen=True)
class Operation:
  """The latest admitted operation. `effect_bound_seconds` is how long work it launched can still run; `outcome` and
  `finished_at` stay None until it is decided."""
  id: str
  contract: str
  phase: str
  admitted_at: datetime
  effect_bound_seconds: int
  finished_at: datetime | None = None
  input_hash: str | None = None
  baseline: Baseline | None = None
  outcome: str | None = None
  reason: str | None = None
  recovery: Recovery | None = None
  task_ref: str | None = None


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


def encode_record(record: TargetRecord) -> str:
  """The record as one line of JSON."""
  def plain(value):
    if isinstance(value, datetime):
      return value.isoformat()
    if isinstance(value, dict):
      return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
      return [plain(item) for item in value]
    return value
  return json.dumps({"format_version": FORMAT_VERSION, **plain(asdict(record))}, sort_keys=True, separators=(",", ":"))


def decode_record(text: str) -> TargetRecord:
  """The record in `text`; ValueError when it is not a record of this format."""
  def fields(value, required: tuple, optional: tuple = ()) -> dict:
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
      raise ValueError(f"Expected the fields {', '.join(required + optional)}.")
    return value

  def text_of(value, nullable=False, pattern=None):
    if value is None and nullable:
      return None
    if not isinstance(value, str) or not value or (pattern and not re.fullmatch(pattern, value)):
      raise ValueError(f"Invalid value {value!r}.")
    return value

  def choice(value, choices, nullable=False):
    if (value is None and nullable) or value in choices:
      return value
    raise ValueError(f"Invalid value {value!r}: expected one of {', '.join(choices)}.")

  def time(value, nullable=False):
    if value is None and nullable:
      return None
    moment = datetime.fromisoformat(text_of(value))
    if moment.tzinfo is not None:
      raise ValueError(f"Invalid time {value!r}: times are naive UTC.")
    return moment

  def optional(value, decode):
    return None if value is None else decode(value)

  def applied(value):
    value = fields(value, ("deployment_id", "config_hash", "committed_at"))
    return Applied(text_of(value["deployment_id"]), text_of(value["config_hash"], pattern="[0-9a-f]{64}"),
                   time(value["committed_at"]))

  def settled(value):
    value = fields(value, ("operation_id", "runtime_goal", "result", "committed_at", "reason"))
    return Settled(text_of(value["operation_id"]), choice(value["runtime_goal"], GOALS), choice(value["result"], RESULTS),
                   time(value["committed_at"]), text_of(value["reason"], nullable=True))

  def baseline(value):
    value = fields(value, ("settled_operation_id", "runtime", "deployment_id", "docker_ids"))
    if not isinstance(value["docker_ids"], list):
      raise ValueError("docker_ids must be a list.")
    return Baseline(text_of(value["settled_operation_id"], nullable=True), choice(value["runtime"], GOALS),
                    text_of(value["deployment_id"], nullable=True), tuple(text_of(item) for item in value["docker_ids"]))

  def recovery(value):
    value = fields(value, ("result", "reason"))
    return Recovery(choice(value["result"], RECOVERIES), text_of(value["reason"], nullable=True))

  def operation(value):
    value = fields(value, ("id", "contract", "phase", "admitted_at", "effect_bound_seconds", "finished_at", "input_hash",
                           "baseline", "outcome", "reason", "recovery", "task_ref"))
    bound = value["effect_bound_seconds"]
    if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
      raise ValueError(f"Invalid effect bound {bound!r}.")
    decoded = Operation(
        text_of(value["id"]), choice(value["contract"], CONTRACTS), choice(value["phase"], PHASES), time(value["admitted_at"]),
        bound, time(value["finished_at"], nullable=True), text_of(value["input_hash"], nullable=True, pattern="[0-9a-f]{64}"),
        optional(value["baseline"], baseline), choice(value["outcome"], OUTCOMES, nullable=True),
        text_of(value["reason"], nullable=True), optional(value["recovery"], recovery), text_of(value["task_ref"], nullable=True))
    if (decoded.phase == "settled") != (decoded.outcome is not None and decoded.finished_at is not None):
      raise ValueError("Only a settled operation has an outcome and a finish.")
    if decoded.phase == "recovering" and decoded.outcome not in ("failed", "interrupted"):
      raise ValueError("A recovering operation has failed or been interrupted.")
    if (decoded.contract == "deploy") != (decoded.input_hash is not None):
      raise ValueError("A deploy, and only a deploy, applies an input fingerprint.")
    if (decoded.contract == "backup_preserve") != (decoded.baseline is not None):
      raise ValueError("An automatic backup, and only one, keeps a baseline.")
    return decoded

  try:
    value = json.loads(text)
  except json.JSONDecodeError as error:
    raise ValueError(f"Not JSON: {error}.") from error
  value = fields(value, ("format_version", "authority", "applied", "settled", "operation"))
  if value["format_version"] != FORMAT_VERSION or isinstance(value["format_version"], bool):
    raise ValueError(f"Unknown format version {value['format_version']!r}.")
  authority = fields(value["authority"], ("instance_id", "state_generation", "container_id"))
  return TargetRecord(
      Authority(*(text_of(authority[key], pattern=OPAQUE_ID) for key in ("instance_id", "state_generation", "container_id"))),
      optional(value["applied"], applied), optional(value["settled"], settled), optional(value["operation"], operation))


def parse_record_listing(lines: list[str]) -> dict[str, TargetRead]:
  """Each placement's record from `grep -H .` over the records (`<dir>/<container id>/record.json:<line>`), by
  container id: present, or invalid when it is not one record of that container. A placement without a record is
  not listed."""
  found: dict[str, list[str]] = {}
  for line in lines:
    path, separator, content = line.partition(":")
    match = re.fullmatch(rf".*/({OPAQUE_ID})/record\.json", path)
    if separator and match:
      found.setdefault(match[1], []).append(content)
  reads = {}
  for container_id, contents in found.items():
    try:
      if len(contents) != 1:
        raise ValueError("A record is one line.")
      record = decode_record(contents[0])
      if record.authority.container_id != container_id:
        raise ValueError("The record belongs to another container.")
      reads[container_id] = TargetRead("present", record)
    except ValueError:
      reads[container_id] = TargetRead("invalid")
  return reads
