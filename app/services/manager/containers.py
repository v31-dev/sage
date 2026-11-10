"""Container lifecycle requests and the operations they queue: Deploy and Stop of one container, which share its
lifecycle slot, its Delete, Deploy all and Stop all of an application's containers, and Cancel of a queued request;
and each container's state, with what tells its pages that it changed."""
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta
from time import monotonic

import config
from services.applications.commands import detach_container
from services.applications.deploy_input import DeployConfig, capture_deploy_config
from services.applications.updates import container_changed
from services.containers.decisions import PROGRESS, RUNNING, Runtime, RuntimeReading, correction, lifecycle_refusal
from services.containers.placement import (LABEL_APPLICATION, LABEL_CONTAINER, LABEL_DEPLOYMENT, LABEL_GENERATION, LABEL_INSTANCE, labels_authority,
                                           parse_placements, placement_dir, placements_command, records_command, runtime_name, runtime_owner,
                                           volume_dir)
from services.containers.record import Applied, Authority, Operation, Recovery, Settled, TargetRead, TargetRecord, parse_record_listing
from services.containers.rendering import render_placement
from services.containers.state import ContainerState, container_state
from services.containers.targets import Containers
from services.db.models import Application, Container, Domain, Instance, Project, Worker
from services.history import record_container_event
from services.logs import Logs
from services.tailscale import Tailscale
from services.updates import Fact
from services.worker_reports import WorkerReports
from utils.common import OPAQUE_ID, new_id, utcnow
from utils.errors import Conflict
from utils.executor import LOGS_EXECUTOR, run_in_executor_with_context
from utils.logging import TaskFailed, task_id
from utils.queue import Added, OnConflict, Outcome
from values.names import qualified_name

logger = logging.getLogger(__name__)

# Each request's key and admission. Deploy and Stop share the container's lifecycle slot: the latest waiting request
# wins and none is taken while one runs. A Delete outranks them: it cancels the container's queued tasks and waits for
# the one running.
REQUESTS = {"deploy": ("lifecycle", OnConflict.REVISE), "stop": ("lifecycle", OnConflict.REVISE),
            "delete": ("delete", OnConflict.REPLACE)}

# Why a reset stopped a container, as its log marker says it.
RESETS = {
    "restored": "its record is from before a platform restore",
    "unexpected_running": "it ran after it was stopped",
    "unexpected_deployment": "it ran a deployment it was not deployed with",
    "no_record": "it ran without a record",
}

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
  def container_states(self, containers) -> dict[str, ContainerState]:
    """The state of each of `containers` now, by id, each application's inputs read once."""
    live = self._live_operations()
    instance = Instance.get()
    hashes, states = {}, {}
    for container in containers:
      if container.application_id not in hashes:
        try:
          hashes[container.application_id] = capture_deploy_config(container.application_id).content_hash
        except LookupError:
          hashes[container.application_id] = None
      states[container.id] = container_state(container, instance, live.get(container.id), hashes[container.application_id])
    return states

  def containers_queue_facts(self) -> list[Fact]:
    """The operations running on containers: a container whose operation starts or ends is shown again."""
    previous = frozenset()

    def changed(current):
      nonlocal previous
      affected = {container_id for container_id, _ in current ^ previous}
      previous = current
      for container in Container.select().where(Container.id.in_(affected)):
        container_changed(container)

    return [Fact(lambda: frozenset(self._live_operations().items()), on_change=changed)]

  def containers_state_fact(self) -> Fact:
    """Every container's state, checked once a minute: a container whose state changed with time alone (its report
    grown stale, say) is shown again, and the domains of one that became routable or stopped being are applied."""
    previous = {}

    def changed(current):
      nonlocal previous
      affected = {container_id for container_id in current.keys() | previous.keys()
                  if current.get(container_id) != previous.get(container_id)}
      rerouted = {container_id for container_id in affected if {
          state.routable for state in (previous.get(container_id), current.get(container_id)) if state is not None} == {True, False}}
      previous = current
      for container in Container.select().where(Container.id.in_(affected)):
        container_changed(container)
      self.request_domain_applies(domain.id for domain in Domain.select(Domain.id).where(Domain.container.in_(rerouted)))
      # TODO(36 step 4E): request discovery when a container's `routable` changed.

    return Fact(lambda: self.container_states(Container.select()), on_change=changed)

  def containers_reported(self, hostname: str, names):
    """A report from the worker changed these runtimes: their containers are shown again."""
    ids = {runtime_owner(name) for name in names} - {None}
    for container in Container.select().where(Container.id.in_(ids) & (Container.worker == hostname)):
      container_changed(container)

  def request_container_observation(self):
    """Queues the minutely look at every container's state."""
    self.add_task(task=self.observe_containers, key=("observe_containers",), scopes=set(), executor="app", quiet=True)

  async def observe_containers(self):
    """Tells the pages of the containers whose state changed, then asks for what reconciliation does about each."""
    self.container_state.check()
    instance = Instance.get()
    containers = {container.id: container for container in
                  Container.select(Container, Application, Project).join(Application).join(Project)}
    reporting = {}
    for container_id, state in self.container_state.last.items():
      container = containers.get(container_id)
      if container is None:
        continue
      if container.worker_id not in reporting:
        reporting[container.worker_id] = WorkerReports().current(container.worker_id) is not None
      found = correction(state.target, state.runtime, state.live,
                         Authority(instance.id, instance.state_generation, container.id), reporting[container.worker_id])
      if found is None or found.kind != "recover":
        self.recoveries.pop(container.id, None)
      if found is None:
        continue
      if found.kind == "read":
        self.request_record_read(container)
      elif found.kind == "reset":
        self.request_reset(container, found.cause)
      else:
        operation = state.target.record.operation if state.target.record is not None else None
        incident = operation.id if operation is not None else "unreadable"
        bound = (operation.effect_bound_seconds if operation is not None
                 else config.CONTAINER_UP_TIMEOUT_SECONDS + config.COMMAND_KILL_AFTER_SECONDS)
        seen = self.recoveries.get(container.id)
        if seen is None or seen[0] != incident:
          self.recoveries[container.id] = (incident, monotonic())
          self.request_recovery(container, incident, final=False)
        elif monotonic() - seen[1] >= bound:
          self.request_recovery(container, incident, final=True)
    for container_id in self.recoveries.keys() - containers.keys():
      self.recoveries.pop(container_id, None)

    # Runtimes of this Sage's that no container owns any more, on the workers that report.
    placed = {}
    for container in containers.values():
      placed.setdefault(container.worker_id, set()).add(container.id)
    for worker in Worker.select():
      received = WorkerReports().current(worker.hostname)
      for name, reading in (received.snapshot.containers.items() if received is not None else ()):
        orphan, owner = runtime_owner(name), labels_authority(reading.labels)
        if orphan and orphan not in placed.get(worker.hostname, ()) and owner is not None and owner.instance_id == instance.id:
          self.request_orphan_cleanup(worker.hostname, orphan, reading.labels.get(LABEL_APPLICATION))

  def request_recovery(self, container: Container, incident: str, final: bool):
    """Queues the recovery of the operation `incident` on the container (or of its unreadable record): its
    containment, or once the old work's allowance has passed its settlement."""
    self.add_task(task=self.recover_container, key=("recover", container.id),
                  scopes={f"worker:{container.worker_id}:container:{container.id}"}, executor="app",
                  params={"container_id": container.id, "incident": incident, "final": final}, labels=self._labels(container))

  def request_reset(self, container: Container, cause: str):
    """Queues the reset of a container whose runtime contradicts its record."""
    self.add_task(task=self.reset_container, key=("reset", container.id),
                  scopes={f"worker:{container.worker_id}:container:{container.id}"}, executor="app",
                  params={"container_id": container.id, "cause": cause}, labels=self._labels(container))

  async def recover_container(self, container_id: str, incident: str, final: bool):
    """Recovers from an operation on the container that never settled, or from its unreadable record, once no task
    owns it: stops the container and keeps the record recovering with the old work's allowance; once that allowance
    has passed (`final`), stops it again and settles the record stopped, failed by the interruption. Nothing is done
    when the record no longer shows that incident."""
    container = Container.get_or_none(Container.id == container_id)
    if container is None:
      logger.info(f"Container {container_id} was deleted before its recovery; nothing to do.")
      return
    instance = Instance.get()
    authority = Authority(instance.id, instance.state_generation, container.id)
    target = await Containers().read(container)
    record = target.record if target.kind == "present" else None
    operation = record.operation if record is not None else None
    if target.kind == "invalid":
      found = "unreadable"
    elif record is not None and record.authority.instance_id == instance.id and operation is not None and operation.phase != "settled":
      found = operation.id
    else:
      found = None
    if found != incident:
      logger.info(f"Container {container.id} no longer needs the recovery of {incident}; nothing to do.")
      return
    contract = operation.contract if operation is not None else "reset"
    try:
      verified_at = await self._stop_runtime(container.worker_id, container.id)
    except Exception as error:
      record_container_event(container, contract, "recovery_failed", operation.id if operation else None,
                             reason={"error": str(error)})
      logger.error(f"The recovery could not stop the container: {error}")
      raise TaskFailed()
    now = utcnow()
    if operation is None:
      operation = Operation(new_id(), "reset", "recovering", now, config.CONTAINER_UP_TIMEOUT_SECONDS + config.COMMAND_KILL_AFTER_SECONDS,
                            outcome="interrupted", reason="its record could not be read", recovery=Recovery("unresolved"),
                            task_ref=task_id.get() or None)
    if not final:
      await Containers().write(container, TargetRecord(
          record.authority if record is not None else authority, record.applied if record else None,
          record.settled if record else None,
          replace(operation, phase="recovering", outcome=operation.outcome or "interrupted", recovery=Recovery("unresolved"))))
      record_container_event(container, contract, "interrupted", operation.id, dedupe_key=f"{operation.id}:interrupted")
      await mark(container, "Stopped: an earlier operation on it was interrupted")
      return
    reason = operation.reason or "interrupted"
    kept = record if record is not None and record.authority == authority else None
    await Containers().write(container, TargetRecord(
        authority, kept.applied if kept else None, Settled(operation.id, "stopped", "failed", now, reason),
        replace(operation, phase="settled", outcome=operation.outcome or "interrupted", finished_at=now,
                verified_on_worker_at=verified_at, recovery=Recovery("verified"))))
    record_container_event(container, contract, "recovered", operation.id, dedupe_key=f"{operation.id}:recovered")
    await mark(container, "Stopped and settled after an interrupted operation")
    # TODO(36 step 4H): self.notify(f"Container {container.id} recovered after an interrupted {contract}.", "warning")

  async def reset_container(self, container_id: str, cause: str):
    """Stops a container whose runtime contradicts its record, once its record and its runtime, read again now, still
    call for it, and records the cause as its failure."""
    container = Container.get_or_none(Container.id == container_id)
    if container is None:
      logger.info(f"Container {container_id} was deleted before its reset; nothing to do.")
      return
    instance = Instance.get()
    authority = Authority(instance.id, instance.state_generation, container.id)
    target = await Containers().read(container)
    reading, _ = await self._inspect(container.worker_id, container.id)
    found = correction(target, Runtime("known", reading), None, authority, True)
    if found is None or found.kind != "reset":
      logger.info(f"Container {container.id} no longer needs a reset; nothing to do.")
      return
    kept = target.record if target.record is not None and target.record.authority == authority else None
    operation = Operation(new_id(), "reset", "executing", utcnow(),
                          config.CONTAINER_DOWN_TIMEOUT_SECONDS + config.COMMAND_KILL_AFTER_SECONDS, task_ref=task_id.get() or None)
    record = TargetRecord(authority, kept.applied if kept else None, kept.settled if kept else None, operation)
    record_container_event(container, "reset", "admitted", operation.id, reason={"cause": found.cause},
                           dedupe_key=f"{operation.id}:admitted")
    try:
      await Containers().write(container, record)
      verified_at = await self._stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "failed", now, found.cause),
          operation=replace(operation, phase="settled", outcome="succeeded", reason=found.cause, finished_at=now,
                            verified_on_worker_at=verified_at)))
    except Exception as error:
      await self._fail(container, record, error)
    record_container_event(container, "reset", "succeeded", operation.id, reason={"cause": found.cause},
                           dedupe_key=f"{operation.id}:succeeded")
    await mark(container, f"Stopped: {RESETS[found.cause]}")
    # TODO(36 step 4H): self.notify(f"Container {container.id} stopped: {RESETS[found.cause]}.", "warning")

  def request_record_read(self, container: Container):
    """Queues a read of the container's record from its worker, under its scope so it never crosses a write."""
    self.add_task(
        task=self.read_record, key=("read_record", container.id), scopes={f"worker:{container.worker_id}:container:{container.id}"},
        executor="app", params={"container_id": container.id}, labels=self._labels(container), quiet=True)

  async def read_record(self, container_id: str):
    container = Container.get_or_none(Container.id == container_id)
    if container is None:
      logger.info(f"Container {container_id} was deleted before its record was read; nothing to do.")
      return
    await Containers().read(container)

  def request_record_listings(self):
    """Queues, for each worker that reports, a listing of the container records on it."""
    for worker in Worker.select():
      if WorkerReports().current(worker.hostname) is not None:
        self.add_task(task=self.list_records, key=("list_records", worker.hostname), scopes=set(), executor="app",
                      params={"hostname": worker.hostname}, labels={"Worker": worker.hostname}, quiet=True)

  async def list_records(self, hostname: str):
    """Lists every container record on the worker and asks for a read of each container whose listed record is not
    the one held; the listing itself never replaces what the `Containers` owner holds, since it is taken outside the
    containers' scopes."""
    try:
      _, lines = await Tailscale().exec_command(hostname, placements_command(config.WORKER_HOME_DIR), timeout=30)
      directories, listed = parse_placements(lines)
    except Exception as error:
      logger.warning(f"Could not list the container placements on {hostname}: {error}")
      return
    placed = set()
    for container in (Container.select(Container, Application, Project).join(Application).join(Project)
                      .where(Container.worker == hostname)):
      placed.add(container.id)
      if listed.get(container.id, TargetRead("absent")) != Containers().target(container.id):
        self.request_record_read(container)
    instance_id = Instance.get().id
    orphans = {container_id for container_id in (directories | listed.keys()) - placed
               if listed.get(container_id, TargetRead("absent")).record is None
               or listed[container_id].record.authority.instance_id == instance_id}
    for container_id in orphans:
      self.request_orphan_cleanup(hostname, container_id)
    for key in [key for key in self.orphans if key[0] == hostname and key[1] not in orphans]:
      self.orphans.pop(key, None)

  def request_orphan_cleanup(self, hostname: str, container_id: str, application: str | None = None):
    """Queues the removal of what this Sage left on the worker for a container it has no row for, unless it is still
    waiting out an operation its record shows unsettled."""
    seen = self.orphans.get((hostname, container_id))
    if seen is not None and monotonic() - seen[1] < seen[2]:
      return
    self.add_task(task=self.cleanup_orphan, key=("cleanup_orphan", hostname, container_id),
                  scopes={f"worker:{hostname}:container:{container_id}"}, executor="app",
                  params={"hostname": hostname, "container_id": container_id},
                  labels={"Worker": hostname, "Container": container_id, **({"Application": application} if application else {})})

  async def cleanup_orphan(self, hostname: str, container_id: str):
    """Removes what this Sage left on the worker for a container it has no row for: its runtime and its placement,
    volume data included. Nothing is done while the row is there, or when its record or runtime is another Sage's; an
    operation its record shows unsettled (any, when the record is unreadable) is first waited out from when Sage saw it."""
    if Container.select().where((Container.id == container_id) & (Container.worker == hostname)).exists():
      logger.info(f"Container {container_id} is on {hostname} again; nothing to clean up.")
      return
    instance_id = Instance.get().id
    _, lines = await Tailscale().exec_command(hostname, records_command(config.WORKER_HOME_DIR, container_id), timeout=30)
    target = parse_record_listing(lines).get(container_id, TargetRead("absent"))
    reading, _ = await self._inspect(hostname, container_id)
    if any(authority is not None and authority.instance_id != instance_id
           for authority in (target.record.authority if target.record else None, reading.authority)):
      logger.info(f"The placement of {container_id} on {hostname} is another Sage's; it is left alone.")
      return
    operation = target.record.operation if target.record else None
    if target.kind == "invalid" or (operation is not None and operation.phase != "settled"):
      incident = operation.id if target.kind == "present" else "unreadable"
      bound = (operation.effect_bound_seconds if target.kind == "present"
               else config.CONTAINER_UP_TIMEOUT_SECONDS + config.COMMAND_KILL_AFTER_SECONDS)
      seen = self.orphans.get((hostname, container_id))
      if seen is None or seen[0] != incident:
        self.orphans[(hostname, container_id)] = (incident, monotonic(), bound)
        logger.info(f"The orphan {container_id} on {hostname} waits up to {bound} s for its earlier work to end.")
        return
      if monotonic() - seen[1] < seen[2]:
        return
    try:
      await self._stop_runtime(hostname, container_id, "--volumes --rmi local --remove-orphans")
      await Tailscale().exec_command(hostname, f"rm -rf {placement_dir(config.WORKER_HOME_DIR, container_id)}", timeout=30)
    except Exception as error:
      logger.error(f"The orphan {container_id} on {hostname} could not be removed: {error}")
      raise TaskFailed()
    self.orphans.pop((hostname, container_id), None)
    logger.info(f"Removed the orphan {container_id} from {hostname}.")
    # TODO(36 step 4H): self.notify(f"Removed an orphaned container from worker {hostname}.", "warning")

  def _live_operations(self) -> dict[str, str]:
    """The operation running on each container that shows its progress, by container id."""
    return {task["params"]["container_id"]: task["name"] for task in self.task_snapshot()["running"]
            if task["name"] in PROGRESS and "container_id" in task["params"]}

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

  def request_delete(self, container_id: str, force: bool = False) -> str:
    """Queues a Delete of the container, after cancelling its queued tasks; with `force`, a worker that can't be
    reached is left as it is. The task's id."""
    return self._admitted(self._request_lifecycle(
        self._lifecycle_container(container_id), "delete", self.delete_container, {}, {"force": force}))

  def request_cancel(self, container_id: str, operation: str) -> str:
    """Cancels the container's queued `operation` (deploy, stop or delete); the cancelled task's id."""
    if operation not in REQUESTS:
      raise ValueError(f"Unknown operation {operation!r}.")
    key = (REQUESTS[operation][0], container_id)
    cancelled = self.cancel_tasks(key, operation)
    if cancelled:
      return cancelled[0]
    if self.has_task(key, running_only=True):
      raise Conflict(f"The {operation} of this container has already started.")
    raise LookupError(f"This container has no queued {operation}.")

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
    if any(self.has_task((slot, container.id)) for container in containers for slot in ("lifecycle", "delete")):
      raise Conflict("A container of this application has an operation in progress.")
    return containers

  def _request_lifecycle(self, container: Container, name: str, task, inputs: dict, params: dict | None = None) -> Added:
    slot, on_conflict = REQUESTS[name]
    scope = f"worker:{container.worker_id}:container:{container.id}"
    if name == "delete":
      self.cancel_tasks(scope=scope)
    elif self.has_task(("delete", container.id)):
      raise Conflict("This container is being deleted.")
    return self.add_task(
        task=task, key=(slot, container.id), name=name, scopes={scope}, executor="app",
        params={"container_id": container.id, **(params or {})}, inputs=inputs, labels=self._labels(container),
        on_conflict=on_conflict)

  @staticmethod
  def _labels(container: Container) -> dict[str, str]:
    """What people call a container task's subjects."""
    application = container.application
    return {"Application": f"{application.project.label} / {application.label}", "Worker": container.worker_id}

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
      await self._stop_runtime(container.worker_id, container.id)

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
      reading, verified_at = await self._inspect(container.worker_id, container.id)
      if reading.state != "running":
        raise Exception(f"The container is {reading.state} after the deploy.")
      if reading.deployment_id != operation.id:
        raise Exception(f"The container runs deployment {reading.deployment_id}, not {operation.id}.")
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
    self.request_domain_applies(domain.id for domain in container.domains)
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
      verified_at = await self._stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now,
                            verified_on_worker_at=verified_at)))
    except Exception as error:
      await self._fail(container, record, error)
    record_container_event(container, "stop", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Stopped")
    self.request_domain_applies(domain.id for domain in container.domains)
    # TODO(36 step 4H): self.notify(f"Container {container.id} stopped on worker {container.worker_id}.")

  async def delete_container(self, container_id: str, force: bool = False):
    """Stops the container and removes its placement from the worker, then its row. With `force`, a worker that can't
    be reached is left as it is and only the row goes."""
    begun = await self._begin(container_id, "delete", force=force)
    if begun is None:
      return
    container, record = begun
    if record is None:
      record_container_event(container, "delete", "succeeded", reason={"worker": "not cleaned up"})
      await mark(container, "Removed from Sage; the worker was not cleaned up")
    else:
      operation = record.operation
      try:
        await Containers().write(container, record)
        await self._stop_runtime(container.worker_id, container.id, "--volumes --rmi local --remove-orphans")
        await Tailscale().exec_command(
            container.worker_id, f"rm -rf {placement_dir(config.WORKER_HOME_DIR, container.id)}", timeout=30)
      except Exception as error:
        await self._fail(container, record, error)
      record_container_event(container, "delete", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
      await mark(container, "Removed from this worker")
    domains = detach_container(container.id)
    Containers().forget(container.id)
    self.request_domain_applies(domains)
    # TODO(36 step 4E): the routes of its public domains, unlinked, go with the cleanup of the worker's routing files.
    # TODO(36 step 4H): self.notify(f"Container {container.id} deleted from worker {container.worker_id}.")
    logger.info(f"Container {container.id} deleted; {len(domains)} domains unlinked.")

  async def _begin(self, container_id: str, contract: str, input_hash: str | None = None,
                   force: bool = False) -> tuple[Container, TargetRecord | None] | None:
    """The container and the record that begins the operation on it, once its record was read and nothing refuses it;
    None when the container is gone. A refusal is recorded and fails the task, with the record untouched. A forced
    operation on a worker that can't be asked begins with no record."""
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
    if refusal is not None:
      record_container_event(container, contract, "rejected", reason={"refusal": refusal})
      logger.error(f"{contract.capitalize()} refused: {REFUSALS[refusal]}.")
      raise TaskFailed()
    kept = target.record if target.record is not None and target.record.authority == authority else None
    timeout = {"deploy": config.CONTAINER_UP_TIMEOUT_SECONDS, "stop": config.CONTAINER_DOWN_TIMEOUT_SECONDS,
               "delete": config.CONTAINER_DOWN_TIMEOUT_SECONDS}[contract]
    bound = timeout + config.COMMAND_KILL_AFTER_SECONDS
    earlier = target.record.operation if target.record is not None else None
    # Taking over from work that may still run (only a Delete does), the record keeps that work's allowance; an
    # unreadable record's is unknown, so the longest one's.
    if target.kind == "invalid":
      bound = max(bound, config.CONTAINER_UP_TIMEOUT_SECONDS + config.COMMAND_KILL_AFTER_SECONDS)
    elif earlier is not None and earlier.phase != "settled":
      bound = max(bound, earlier.effect_bound_seconds)
    operation = Operation(new_id(), contract, "executing", utcnow(), bound,
                          input_hash=input_hash, task_ref=task_id.get() or None)
    record_container_event(container, contract, "admitted", operation.id, dedupe_key=f"{operation.id}:admitted")
    return container, TargetRecord(authority, kept.applied if kept else None, kept.settled if kept else None, operation)

  async def _fail(self, container: Container, record: TargetRecord, error: Exception):
    """After a failure of a begun operation: stops the container, settles the record stopped with the failure, or
    leaves it recovering when the stop is not verified, then fails the task."""
    operation, reason = record.operation, str(error) or type(error).__name__
    logger.error(f"{operation.contract.capitalize()} failed: {reason}")
    try:
      verified_at = await self._stop_runtime(container.worker_id, container.id)
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
    self.request_domain_applies(domain.id for domain in container.domains)
    # TODO(36 step 4H): self.notify(f"... {operation.contract} failed on worker {container.worker_id}: {reason}", "error")
    raise TaskFailed()

  async def _stop_runtime(self, hostname: str, container_id: str, options: str = "") -> datetime:
    """Stops the container's runtime on the worker (`options` for `docker compose down`); when the worker verified it
    stopped, on the worker's clock. Raises when it was not stopped."""
    await Tailscale().exec_command(hostname, f"docker compose -p {runtime_name(container_id)} down {options}".strip(),
                                   timeout=config.CONTAINER_DOWN_TIMEOUT_SECONDS)
    reading, verified_at = await self._inspect(hostname, container_id)
    if reading.state in RUNNING:
      raise Exception(f"The container is still {reading.state} after it was stopped.")
    return verified_at

  async def _inspect(self, hostname: str, container_id: str) -> tuple[RuntimeReading, datetime]:
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
