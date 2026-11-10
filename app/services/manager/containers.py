"""Container lifecycle requests and the operations they queue: Deploy and Stop of one container, which share its
lifecycle slot, Deploy all and Stop all of an application's containers, and Cancel of a queued request."""
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta

import config
from services.applications.deploy_input import DeployConfig, capture_deploy_config
from services.containers.decisions import RUNNING, lifecycle_refusal
from services.containers.placement import LABEL_DEPLOYMENT, placement_dir, runtime_name, volume_dir
from services.containers.record import Applied, Authority, Operation, Settled, TargetRecord
from services.containers.rendering import render_placement
from services.containers.targets import Containers
from services.db.models import Application, Container, Instance, Project
from services.history import record_container_event
from services.logs import Logs
from services.tailscale import Tailscale
from utils.common import OPAQUE_ID, new_id, utcnow
from utils.errors import Conflict
from utils.executor import LOGS_EXECUTOR, run_in_executor_with_context
from utils.logging import TaskFailed, task_id
from utils.queue import Added, OnConflict, Outcome
from values.names import qualified_name

logger = logging.getLogger(__name__)

REFUSALS = {
    "unreachable": "its worker can't be reached",
    "invalid_record": "its record on the worker can't be read",
    "authority": "its placement on the worker belongs to another Sage",
    "interrupted": "an earlier operation on it is still being recovered",
}


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


class ContainersMixin:
  def request_deploy(self, container_id: str) -> str:
    """Queues a Deploy of the container with its application's inputs as they are now; the task's id."""
    container = self._lifecycle_container(container_id)
    deploy = capture_deploy_config(container.application_id)
    deploy.check_deployable()
    return self._admitted(self._request_lifecycle(container, "deploy", self.deploy_container, {"deploy": deploy}))

  def request_stop(self, container_id: str) -> str:
    """Queues a Stop of the container; the task's id."""
    return self._admitted(self._request_lifecycle(self._lifecycle_container(container_id), "stop", self.stop_container, {}))

  def request_deploy_all(self, application_id: int) -> list[str]:
    """Queues a Deploy of each of the application's containers, all with its inputs as they are now; the ids of the
    tasks queued."""
    containers = self._application_containers(application_id)
    deploy = capture_deploy_config(application_id)
    deploy.check_deployable()
    added = [self._request_lifecycle(container, "deploy", self.deploy_container, {"deploy": deploy})
             for container in containers]
    return [result.task_id for result in added if result.outcome == Outcome.ACCEPTED]

  def request_stop_all(self, application_id: int) -> list[str]:
    """Queues a Stop of each of the application's containers; the ids of the tasks queued."""
    added = [self._request_lifecycle(container, "stop", self.stop_container, {})
             for container in self._application_containers(application_id)]
    return [result.task_id for result in added if result.outcome == Outcome.ACCEPTED]

  def request_cancel(self, container_id: str) -> str:
    """Cancels the container's queued lifecycle request; the cancelled task's id."""
    cancelled = self.cancel_tasks(("lifecycle", container_id))
    if cancelled:
      return cancelled[0]
    if self.has_task(("lifecycle", container_id), running_only=True):
      raise Conflict("The operation on this container has already started.")
    raise LookupError("This container has no queued operation.")

  def _lifecycle_container(self, container_id: str) -> Container:
    container = (Container.select(Container, Application, Project).join(Application).join(Project)
                 .where(Container.id == container_id).first())
    if container is None:
      raise LookupError("Container not found.")
    return container

  def _application_containers(self, application_id: int) -> list[Container]:
    """The application's containers, for a request on all of them: refused when it has none, or while any of them has
    a lifecycle request queued or running."""
    containers = list(Container.select(Container, Application, Project).join(Application).join(Project)
                      .where(Container.application == application_id).order_by(Container.worker))
    if not containers:
      raise ValueError("This application has no containers.")
    if any(self.has_task(("lifecycle", container.id)) for container in containers):
      raise Conflict("A container of this application has an operation in progress.")
    return containers

  def _request_lifecycle(self, container: Container, name: str, task, inputs: dict) -> Added:
    application = container.application
    return self.add_task(
        task=task, key=("lifecycle", container.id), name=name,
        scopes={f"worker:{container.worker_id}:container:{container.id}"}, executor="app",
        params={"container_id": container.id}, inputs=inputs,
        labels={"Application": f"{application.project.label} / {application.label}", "Worker": container.worker_id},
        on_conflict=OnConflict.REVISE)

  @staticmethod
  def _admitted(added: Added) -> str:
    if added.outcome == Outcome.BUSY:
      raise Conflict("This container already has an operation in progress.")
    if added.outcome == Outcome.REFUSED:
      raise Conflict("Sage is stopping.")
    return added.task_id

  async def deploy_container(self, container_id: str, deploy: DeployConfig):
    """Stops the container, writes the deployment's files, starts it and verifies it runs this deployment."""
    begun = await self._begin(container_id, "deploy", deploy.content_hash)
    if begun is None:
      return
    container, record = begun
    operation, host, home = record.operation, container.worker_id, config.WORKER_HOME_DIR
    directory = placement_dir(home, container.id)
    try:
      await Containers().write(container, record)
      await self._stop_runtime(container)

      files = render_placement(deploy, record.authority, operation.id, host, home)
      secrets = any(file.path.startswith("secrets/") for file in files)
      await Tailscale().exec_command(
          host, f"rm -rf {directory}/secrets" + (f" && mkdir -m 700 {directory}/secrets" if secrets else ""), timeout=30)
      for file in files:
        await Tailscale().sync_file(host, config.TEMPLATES_DIR / "worker/file", f"{directory}/{file.path}",
                                    {"CONTENT": file.content}, mode=file.mode)
      kept = [volume_id for volume_id, _ in deploy.volumes]
      _, present = await Tailscale().exec_command(
          host, f"mkdir -p {directory}/volumes && ls -1 {directory}/volumes", timeout=30)
      # Only Sage's own volume names, so a directory planted on the worker can't put text into the command.
      pruned = [name for name in present if re.fullmatch(OPAQUE_ID, name) and name not in kept]
      await Tailscale().exec_command(host, "; ".join(
          [f"mkdir -p {volume_dir(home, container.id, volume_id)}" for volume_id in kept]
          + [f"rm -rf {directory}/volumes/{name}" for name in pruned]) or "true", timeout=60)

      await Tailscale().exec_command(
          host, f"docker compose -f {directory}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --build",
          timeout=config.CONTAINER_UP_TIMEOUT_SECONDS)
      state, deployment, verified_at = await self._inspect(container)
      if state != "running":
        raise Exception(f"The container is {state} after the deploy.")
      if deployment != operation.id:
        raise Exception(f"The container runs deployment {deployment}, not {operation.id}.")
      now = utcnow()
      await Containers().write(container, replace(
          record, applied=Applied(operation.id, deploy.content_hash, now),
          settled=Settled(operation.id, "running", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now,
                            verified_on_worker_at=verified_at)))
    except Exception as error:
      await self._fail(container, record, error)
    record_container_event(container, "deploy", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Deployed")
    # TODO(36 step 4E): self.request_domain_syncs(Domain.select().where(Domain.container == container.id))
    # TODO(36 step 4H): self.notify(f"Application {deploy.qualified_name} container deployed to worker {host}.")

  async def stop_container(self, container_id: str):
    """Stops the container and verifies nothing of it runs."""
    begun = await self._begin(container_id, "stop")
    if begun is None:
      return
    container, record = begun
    operation = record.operation
    try:
      await Containers().write(container, record)
      verified_at = await self._stop_runtime(container)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now,
                            verified_on_worker_at=verified_at)))
    except Exception as error:
      await self._fail(container, record, error)
    record_container_event(container, "stop", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Stopped")
    # TODO(36 step 4E): self.request_domain_syncs(Domain.select().where(Domain.container == container.id))
    # TODO(36 step 4H): self.notify(f"Container {container.id} stopped on worker {container.worker_id}.")

  async def _begin(self, container_id: str, contract: str,
                   input_hash: str | None = None) -> tuple[Container, TargetRecord] | None:
    """The container and the record that begins the operation on it, once its record was read and nothing refuses it;
    None when the container is gone. A refusal is recorded and fails the task, with the record untouched."""
    container = Container.get_or_none(Container.id == container_id)
    if container is None:
      logger.info(f"Container {container_id} was deleted before its {contract}; nothing to do.")
      return None
    instance = Instance.get()
    authority = Authority(instance.id, instance.state_generation, container.id)
    target = await Containers().read(container)
    refusal = lifecycle_refusal(target, authority)
    if refusal is not None:
      record_container_event(container, contract, "rejected", reason={"refusal": refusal})
      logger.error(f"{contract.capitalize()} refused: {REFUSALS[refusal]}.")
      raise TaskFailed()
    kept = target.record if target.record is not None and target.record.authority == authority else None
    timeout = {"deploy": config.CONTAINER_UP_TIMEOUT_SECONDS, "stop": config.CONTAINER_DOWN_TIMEOUT_SECONDS}[contract]
    operation = Operation(new_id(), contract, "executing", utcnow(), timeout + config.COMMAND_KILL_AFTER_SECONDS,
                          input_hash=input_hash, task_ref=task_id.get() or None)
    record_container_event(container, contract, "admitted", operation.id, dedupe_key=f"{operation.id}:admitted")
    return container, TargetRecord(authority, kept.applied if kept else None, kept.settled if kept else None, operation)

  async def _fail(self, container: Container, record: TargetRecord, error: Exception):
    """After a failure of a begun operation: stops the container, settles the record stopped with the failure, or
    leaves it recovering when the stop is not verified, then fails the task."""
    operation, reason = record.operation, str(error) or type(error).__name__
    logger.error(f"{operation.contract.capitalize()} failed: {reason}")
    try:
      verified_at = await self._stop_runtime(container)
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
    await mark(container, f"{operation.contract.capitalize()} failed: {reason}")
    # TODO(36 step 4H): self.notify(f"... {operation.contract} failed on worker {container.worker_id}: {reason}", "error")
    raise TaskFailed()

  async def _stop_runtime(self, container: Container) -> datetime:
    """Stops the container's runtime; when the worker verified it stopped, on the worker's clock. Raises when it was
    not stopped."""
    await Tailscale().exec_command(container.worker_id, f"docker compose -p {runtime_name(container.id)} down",
                                   timeout=config.CONTAINER_DOWN_TIMEOUT_SECONDS)
    state, _, verified_at = await self._inspect(container)
    if state in RUNNING:
      raise Exception(f"The container is still {state} after it was stopped.")
    return verified_at

  async def _inspect(self, container: Container) -> tuple[str, str | None, datetime]:
    """The runtime's Docker state (absent without one) and its deployment, and the worker's time just before it
    looked, rounded up to the second so a report gathered before the look never counts as after it."""
    _, lines = await Tailscale().exec_command(
        container.worker_id,
        f"date -u +%s; docker inspect --format '{{{{.State.Status}}}} {{{{index .Config.Labels \"{LABEL_DEPLOYMENT}\"}}}}' "
        f"{runtime_name(container.id)} 2>/dev/null || echo absent", timeout=30)
    if len(lines) != 2 or not lines[0].strip().isdigit() or not lines[1].strip():
      raise Exception(f"Unexpected answer from the worker: {lines}")
    state, _, deployment = lines[1].strip().partition(" ")
    return (state, deployment if deployment not in ("", "<no value>") else None,
            datetime(1970, 1, 1) + timedelta(seconds=int(lines[0]) + 1))
