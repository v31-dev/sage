"""What the operations on a container share, its own lifecycle and the backups and restores of its volumes: their
admission, refusals and names, and the steps that begin and fail an operation and act on the runtime."""
import logging
from dataclasses import replace
from datetime import datetime, timedelta

import config
from services.containers.decisions import RUNNING, RuntimeReading, lifecycle_refusal
from services.containers.placement import (LABEL_CONTAINER, LABEL_DEPLOYMENT, LABEL_GENERATION, LABEL_INSTANCE, labels_authority, placement_scope,
                                           runtime_name)
from services.containers.record import Authority, Baseline, Operation, Settled, TargetRecord
from services.containers.targets import Containers
from services.db.models import Application, Container, Instance, Project
from services.history import record_container_event
from services.logs import Logs
from services.notification import Notifications
from services.tailscale import Tailscale
from utils.common import new_id, utcnow
from utils.errors import Conflict
from utils.executor import LOGS_EXECUTOR, run_in_executor_with_context
from utils.logging import TaskFailed, task_id
from utils.queue import Added, OnConflict, Outcome
from values.names import qualified_name

logger = logging.getLogger(__name__)

# Each request's key and admission. Deploy, Stop, a manual backup and a volume restore share the container's lifecycle
# slot: the latest waiting request wins and none is taken while one runs. A Delete outranks them: it cancels the
# container's queued tasks and waits for the one running.
REQUESTS = {"deploy": ("lifecycle", OnConflict.REVISE), "stop": ("lifecycle", OnConflict.REVISE),
            "backup_stopped": ("lifecycle", OnConflict.REVISE), "restore_stopped": ("lifecycle", OnConflict.REVISE),
            "delete": ("delete", OnConflict.REPLACE)}

REFUSALS = {
    "unreachable": "its worker can't be reached",
    "invalid_record": "its record on the worker can't be read",
    "authority": "its placement on the worker belongs to another Sage",
    "interrupted": "an earlier operation on it is still being recovered",
    "store_changed": "the backup store is not the one it was requested for",
    "not_deployed": "it was never deployed",
    "running": "it runs, and it must be stopped first",
    "failed": "its last action failed",
    "unexpected_runtime": "its runtime is not what its last action left",
    "no_volumes": "none of the volumes asked for is configured",
    "no_backup": "the backup is no longer listed",
}


# What people call each operation on a container, in log lines and markers.
NAMES = {"deploy": "Deploy", "stop": "Stop", "delete": "Delete", "backup_stopped": "Backup",
         "backup_preserve": "Automatic backup", "restore_stopped": "Restore", "reset": "Reset"}


class Refused(Exception):
  """A pre-check of an operation that refuses it, with the refusal (one of REFUSALS)."""

  def __init__(self, refusal: str):
    super().__init__(refusal)
    self.refusal = refusal


async def mark(container: Container, text: str):
  """Writes into the container's application logs what Sage did to it, with the running task's id. A marker that
  cannot be written is a warning, never the action's failure."""
  task = task_id.get()
  name = qualified_name(container.application.project_id, container.application.name)
  try:
    await run_in_executor_with_context(
        Logs().write_marker, name, container.worker_id, f"{text} (task {task})" if task else text, executor=LOGS_EXECUTOR)
  except Exception:
    logger.warning(f"Could not mark '{text}' in the logs of {name}.", exc_info=True)


class LifecycleMixin:
  def lifecycle_container(self, container_id: str) -> Container:
    """The container, with its application and project, for a request on it."""
    container = (Container.select(Container, Application, Project).join(Application).join(Project)
                 .where(Container.id == container_id).first())
    if container is None:
      raise LookupError("Container not found.")
    return container

  def application_containers(self, application_id: int) -> list[Container]:
    """The application's containers, for a request on all of them: refused when it has none, or while any of them has
    a lifecycle request queued or running."""
    containers = list(Container.select(Container, Application, Project).join(Application).join(Project)
                      .where(Container.application == application_id).order_by(Container.worker))
    if not containers:
      raise ValueError("This application has no containers.")
    if any(self.has_task((slot, container.id)) for container in containers for slot in ("lifecycle", "delete")):
      raise Conflict("A container of this application has an operation in progress.")
    return containers

  def queue_lifecycle(self, container: Container, name: str, task, inputs: dict, params: dict | None = None,
                      scopes: tuple[str, ...] = (), labels: dict[str, str] | None = None) -> Added:
    """Queues the request `name` on the container in its slot (`REQUESTS`); a Delete first cancels the container's
    queued tasks, and any other request is refused while one runs or waits. The queue's answer."""
    slot, on_conflict = REQUESTS[name]
    scope = placement_scope(container.worker_id, container.id)
    if name == "delete":
      self.cancel_tasks(scope=scope)
    elif self.has_task(("delete", container.id)):
      raise Conflict("This container is being deleted.")
    return self.add_task(
        task=task, key=(slot, container.id), name=name, scopes={scope, *scopes}, executor="app",
        params={"container_id": container.id, **(params or {})}, inputs=inputs,
        labels={**self.container_labels(container), **(labels or {})}, on_conflict=on_conflict)

  @staticmethod
  def container_labels(container: Container) -> dict[str, str]:
    """What people call a container task's subjects."""
    application = container.application
    return {"Application": f"{application.project.label} / {application.label}", "Worker": container.worker_id}

  @classmethod
  def container_notice(cls, container: Container) -> tuple[str, str]:
    """What a notice calls the container, "<project> / <application> on <worker>", and the path of its application's
    page."""
    labels, application = cls.container_labels(container), container.application
    return f"{labels['Application']} on {labels['Worker']}", f"/projects/{application.project_id}/{application.name}"

  @staticmethod
  def admitted_task_id(added: Added) -> str:
    """The id of the task that does the request; a Conflict when the container is busy or Sage is stopping or
    restoring."""
    if added.outcome == Outcome.BUSY:
      raise Conflict("This container already has an operation in progress.")
    if added.outcome == Outcome.REFUSED:
      raise Conflict("Sage is stopping or restoring a backup.")
    return added.task_id

  async def begin_lifecycle(self, container_id: str, contract: str, input_hash: str | None = None, force: bool = False,
                            prepare=None) -> tuple[Container, TargetRecord | None] | None:
    """The container and the record that begins the operation on it, once its record was read and nothing refuses it;
    None when the container is gone. `prepare`, awaited with the container and its record under this Sage's current
    authority (None without one), checks what the operation itself needs, raising `Refused`, and gives the baseline
    it records. A refusal is recorded and fails the task, with the record untouched. A forced operation on a worker
    that can't be asked begins with no record."""
    container = Container.get_or_none(Container.id == container_id)
    if container is None:
      logger.info(f"Container {container_id} was deleted before its {contract}; nothing to do.")
      return None
    instance = Instance.get()
    authority = Authority(instance.id, instance.state_generation, container.id)
    target = await Containers().read(container)
    refusal = lifecycle_refusal(target, authority, contract)
    if refusal == "unreachable" and force:
      record_container_event(container, contract, "admitted", reason={"worker": "unreachable"})
      return container, None
    kept = target.record if target.record is not None and target.record.authority == authority else None
    baseline = None
    if refusal is None and prepare is not None:
      try:
        baseline = await prepare(container, kept)
      except Refused as refused:
        refusal = refused.refusal
    if refusal is not None:
      record_container_event(container, contract, "rejected", reason={"refusal": refusal})
      logger.error(f"{NAMES[contract]} refused: {REFUSALS[refusal]}.")
      raise TaskFailed()
    timeout = {"deploy": config.CONTAINER_UP_TIMEOUT_SECONDS, "stop": config.CONTAINER_DOWN_TIMEOUT_SECONDS,
               "delete": config.CONTAINER_DOWN_TIMEOUT_SECONDS, "backup_stopped": config.BACKUP_TIMEOUT_SECONDS,
               "backup_preserve": config.BACKUP_TIMEOUT_SECONDS, "restore_stopped": config.BACKUP_TIMEOUT_SECONDS}[contract]
    bound = timeout + config.COMMAND_KILL_AFTER_SECONDS
    earlier = target.record.operation if target.record is not None else None
    # Taking over from work that may still run (only a Delete does), the record keeps that work's allowance; an
    # unreadable record's is unknown, so the longest one's.
    if target.kind == "invalid":
      bound = max(bound, config.CONTAINER_UP_TIMEOUT_SECONDS + config.COMMAND_KILL_AFTER_SECONDS)
    elif earlier is not None and earlier.phase != "settled":
      bound = max(bound, earlier.effect_bound_seconds)
    operation = Operation(new_id(), contract, "executing", utcnow(), bound,
                          input_hash=input_hash, baseline=baseline, task_ref=task_id.get() or None)
    record_container_event(container, contract, "admitted", operation.id, dedupe_key=f"{operation.id}:admitted")
    return container, TargetRecord(authority, kept.applied if kept else None, kept.settled if kept else None, operation)

  async def fail_lifecycle(self, container: Container, record: TargetRecord, error: Exception):
    """After a failure of a begun operation: stops the container, settles the record stopped with the failure, or
    leaves it recovering when the stop is not verified, then fails the task."""
    operation, reason = record.operation, str(error) or type(error).__name__
    name, page = self.container_notice(container)
    Notifications().notify(f"{NAMES[operation.contract]} of {name} failed: {reason}", "error", page)
    try:
      verified_at = await self.stop_runtime(container.worker_id, container.id)
      now = utcnow()
      failed = replace(record, settled=Settled(operation.id, "stopped", "failed", now, reason), operation=replace(
          operation, phase="settled", outcome="failed", reason=reason, finished_at=now, verified_on_worker_at=verified_at))
    except Exception as stop_error:
      logger.error(f"The container could not be stopped after the failure: {stop_error}")
      failed = replace(record, operation=replace(operation, phase="recovering", outcome="failed", reason=reason))
    try:
      await Containers().write(container, failed)
    except Exception as write_error:
      logger.error(f"The failure could not be recorded on the worker: {write_error}")
    record_container_event(container, operation.contract, "failed", operation.id, reason={"error": reason},
                           dedupe_key=f"{operation.id}:failed")
    await mark(container, f"{NAMES[operation.contract]} failed: {reason}")
    self.request_domain_applies(domain.id for domain in container.domains)
    raise TaskFailed()

  async def stop_runtime(self, hostname: str, container_id: str, options: str = "") -> datetime:
    """Stops the container's runtime on the worker (`options` for `docker compose down`); when the worker verified it
    stopped, on the worker's clock. Raises when it was not stopped."""
    await Tailscale().exec_command(hostname, f"docker compose -p {runtime_name(container_id)} down {options}".strip(),
                                   timeout=config.CONTAINER_DOWN_TIMEOUT_SECONDS)
    reading, verified_at = await self.inspect_runtime(hostname, container_id)
    if reading.state in RUNNING:
      raise Exception(f"The container is still {reading.state} after it was stopped.")
    return verified_at

  async def quiesce_runtime(self, hostname: str, container_id: str):
    """Stops the container's runtime on the worker and keeps it, to be started again as it is. Raises when it was not
    stopped."""
    await Tailscale().exec_command(hostname, f"docker compose -p {runtime_name(container_id)} stop",
                                   timeout=config.CONTAINER_DOWN_TIMEOUT_SECONDS)
    reading, _ = await self.inspect_runtime(hostname, container_id)
    if reading.state in RUNNING:
      raise Exception(f"The container is still {reading.state} after it was stopped.")

  async def resume_runtime(self, hostname: str, container_id: str, baseline: Baseline) -> datetime:
    """Brings the container back to the runtime `baseline` holds: started again as it was, the same Docker containers
    running the same deployment, or still stopped. When the worker verified it, on the worker's clock; raises when it
    is not that runtime."""
    if baseline.runtime == "running":
      await Tailscale().exec_command(hostname, f"docker compose -p {runtime_name(container_id)} start",
                                     timeout=config.CONTAINER_DOWN_TIMEOUT_SECONDS)
    reading, verified_at = await self.inspect_runtime(hostname, container_id)
    if baseline.runtime == "stopped":
      if reading.state in RUNNING:
        raise Exception(f"The container is {reading.state}, though it was stopped before the backup.")
      return verified_at
    if reading.state != "running" or reading.deployment_id != baseline.deployment_id:
      raise Exception(f"The container is {reading.state} with deployment {reading.deployment_id} after it was started again, "
                      f"not running {baseline.deployment_id}.")
    if await self.runtime_ids(hostname, container_id) != baseline.docker_ids:
      raise Exception("Other Docker containers run than those stopped for the backup.")
    return verified_at

  async def runtime_ids(self, hostname: str, container_id: str) -> tuple[str, ...]:
    """Docker's ids of the containers of the runtime that run, sorted."""
    _, lines = await Tailscale().exec_command(hostname, f"docker compose -p {runtime_name(container_id)} ps -q", timeout=30)
    return tuple(sorted(line.strip() for line in lines if line.strip()))

  async def inspect_runtime(self, hostname: str, container_id: str) -> tuple[RuntimeReading, datetime]:
    """The runtime as Docker has it now (absent without one: its state, deployment and the authority its labels name),
    and the worker's time just before it looked, rounded up to the second so a report gathered before the look never
    counts as after it."""
    labels = (LABEL_DEPLOYMENT, LABEL_INSTANCE, LABEL_GENERATION, LABEL_CONTAINER)
    fields = "|".join(["{{.State.Status}}", *(f'{{{{index .Config.Labels "{label}"}}}}' for label in labels)])
    _, lines = await Tailscale().exec_command(
        hostname, f"date -u +%s; docker inspect --format '{fields}' {runtime_name(container_id)} 2>/dev/null || echo absent",
        timeout=30)
    parts = lines[1].strip().split("|") if len(lines) == 2 else []
    if len(lines) != 2 or not lines[0].strip().isdigit() or len(parts) not in (1, len(labels) + 1) or not parts[0]:
      raise Exception(f"Unexpected answer from the worker: {lines}")
    verified_at = datetime(1970, 1, 1) + timedelta(seconds=int(lines[0]) + 1)
    if parts == ["absent"]:
      return RuntimeReading("absent"), verified_at
    values = dict(zip(labels, (None if value in ("", "<no value>") else value for value in parts[1:])))
    return RuntimeReading(parts[0], values[LABEL_DEPLOYMENT], authority=labels_authority(values)), verified_at
