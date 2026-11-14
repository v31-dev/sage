from datetime import datetime

from services.containers.decisions import Runtime, RuntimeReading
from services.containers.placement import LABEL_DEPLOYMENT, labels_authority, runtime_name
from services.containers.record import TargetRecord
from services.worker_reports import Received, is_current


def runtime_of(received: Received | None, container_id: str, record: TargetRecord | None, now: datetime,
               reachable: bool | None) -> Runtime:
  """The container's runtime from the newest look at it: the worker's newest report, or the verification of the
  record's settled operation when no report gathered at or after that verification (both on the worker's clock) has
  arrived yet. That verification saw the settled goal: the applied deployment running under the record's authority,
  or the container stopped. Either look is current for REPORT_STALE_SECONDS after it reached the Manager. A worker
  known to be unreachable makes it unreachable; `reachable` None is not known either way."""
  sample = None
  if received is not None:
    found = received.snapshot.containers.get(runtime_name(container_id))
    sample = RuntimeReading("absent") if found is None else RuntimeReading(
        found.status, found.labels.get(LABEL_DEPLOYMENT), found.health, found.exit_code, found.oom_killed,
        labels_authority(found.labels))
  operation = record.operation if record is not None else None
  verified = operation if operation is not None and operation.phase == "settled" else None
  outcome = None
  if verified is not None and record.settled is not None:
    outcome = (RuntimeReading("running", record.applied.deployment_id if record.applied else None, authority=record.authority)
               if record.settled.runtime_goal == "running" else RuntimeReading("exited", authority=record.authority))

  if reachable is False:
    return Runtime("unreachable", sample or outcome)
  if received is not None and (verified is None or received.snapshot.at >= verified.verified_on_worker_at):
    return Runtime("known" if is_current(received.arrived_at, now) else "stale", sample)
  if outcome is not None:
    return Runtime("known" if is_current(verified.finished_at, now) else "stale", outcome)
  return Runtime("stale", sample) if received is not None else Runtime("unobserved")
