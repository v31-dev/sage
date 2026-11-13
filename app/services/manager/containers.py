"""Container lifecycle requests and the operations they queue: Deploy and Stop of one container, which share its
lifecycle slot, its Delete, Deploy all and Stop all of an application's containers, and Cancel of a queued request;
the recovery of an operation that never settled; and each container's state, with what tells its pages that it
changed."""
import logging
import re
from collections import Counter
from dataclasses import replace
from time import monotonic

import config
from services.applications.commands import detach_container
from services.applications.deploy_input import DeployConfig, capture_deploy_config
from services.applications.lookup import find_application
from services.applications.payload import application_payload, application_summary, project_payload
from services.applications.updates import container_changed
from services.containers.decisions import PROGRESS, Runtime, correction
from services.containers.placement import (LABEL_APPLICATION, labels_authority, parse_placements, placement_dir, placement_scope, placements_command,
                                           records_command, runtime_name, runtime_owner, volume_dir)
from services.containers.record import Applied, Authority, Operation, Recovery, Settled, TargetRead, TargetRecord, parse_record_listing
from services.containers.rendering import render_placement
from services.containers.state import ContainerState, QueuedRequest, container_payload, container_state
from services.containers.targets import Containers
from services.db.models import Application, Container, ContainerEvent, Domain, Instance, Project, Volume, Worker
from services.history import record_container_event
from services.metrics import Metrics
from services.notification import Notifications
from services.tailscale import Tailscale
from services.updates import Fact
from services.worker_reports import WorkerReports
from utils.common import OPAQUE_ID, new_id, utcnow
from utils.errors import Conflict
from utils.logging import TaskFailed, task_id
from utils.queue import Outcome

from .lifecycle import NAMES, REQUESTS, mark

logger = logging.getLogger(__name__)

# Why a reset stopped a container, as its log marker says it.
RESETS = {
    "restored": "its record is from before a platform restore",
    "unexpected_running": "it ran after it was stopped",
    "unexpected_deployment": "it ran a deployment it was not deployed with",
    "no_record": "it ran without a record",
}


class ContainersMixin:
  def container_states(self, containers) -> dict[str, ContainerState]:
    """The state of each of `containers` now, by id, each application's inputs read once."""
    live, queued = self._container_tasks()
    instance = Instance.get()
    hashes, states = {}, {}
    for container in containers:
      if container.application_id not in hashes:
        try:
          hashes[container.application_id] = capture_deploy_config(container.application_id).content_hash
        except LookupError:
          hashes[container.application_id] = None
      states[container.id] = container_state(container, instance, live.get(container.id), queued.get(container.id, ()),
                                             hashes[container.application_id])
    return states

  def container_payloads(self, containers) -> list[dict]:
    """The payload of each of `containers`, in their order, with its domains' payloads and its events."""
    containers = list(containers)
    states = self.container_states(containers)
    ids = [container.id for container in containers]
    domains = list(Domain.select().where(Domain.container.in_(ids)).order_by(Domain.type, Domain.name))
    linked = {}
    for domain, payload in zip(domains, self.domain_payloads(domains)):
      linked.setdefault(domain.container_id, []).append(payload)
    events = {}
    for event in ContainerEvent.select().where(ContainerEvent.container.in_(ids)).order_by(ContainerEvent.id.desc()):
      events.setdefault(event.container_id, []).append(event)
    return [container_payload(container, states[container.id], linked.get(container.id, []), events.get(container.id, []))
            for container in containers]

  def application_page(self, project: str, name: str) -> dict:
    """The application's payload with its containers'; a LookupError when there is none."""
    application = find_application(project, name)
    containers = (Container.select(Container, Application, Project, Worker).join(Application).join(Project)
                  .switch(Container).join(Worker).where(Container.application == application.id).order_by(Container.worker))
    return application_payload(application, list(application.volumes.order_by(Volume.name)), self.container_payloads(containers))

  def project_page(self, project: str) -> dict:
    """The project's payload with its applications' summaries, by name; a LookupError when there is none."""
    row = Project.get_or_none(Project.name == project)
    if row is None:
      raise LookupError("Project not found.")
    containers = list(Container.select(Container, Application, Project).join(Application).join(Project)
                      .where(Application.project == project))
    states = self.container_states(containers)
    active = Counter(container.application_id for container in containers if states[container.id].view.headline == "active")
    return {**project_payload(row), "applications": [
        application_summary(application, active[application.id])
        for application in Application.select().where(Application.project == project).order_by(Application.name)]}

  def application_graphs(self, project: str, name: str, period: str) -> list[dict]:
    """The series of each of the application's containers over `period`, by worker; a LookupError when there is none."""
    application = find_application(project, name)
    return [{"container": container.id, "hostname": container.worker_id,
             "metrics": Metrics().query_container_period(container.worker_id, runtime_name(container.id), period)}
            for container in Container.select().where(Container.application == application.id).order_by(Container.worker)]

  def containers_queue_facts(self) -> list[Fact]:
    """The operations running on containers and the requests queued on them: a container whose operation starts or
    ends, or whose requests are queued, start or are cancelled, is shown again."""
    previous = frozenset()

    def changed(current):
      nonlocal previous
      affected = {container_id for container_id, *_ in current ^ previous}
      previous = current
      for container in Container.select().where(Container.id.in_(affected)):
        container_changed(container)

    def tasks():
      live, queued = self._container_tasks()
      return frozenset((container_id, live.get(container_id), queued.get(container_id, ()))
                       for container_id in live.keys() | queued.keys())

    return [Fact(tasks, on_change=changed)]

  def containers_state_fact(self) -> Fact:
    """Every container's state, checked once a minute: a container whose state changed with time alone (its report
    grown stale, say) is shown again, the domains of one that became routable or stopped being are applied, and
    discovery is published again when whether a container with a service's domain can be routed to changed, its
    becoming known included."""
    previous = {}

    def changed(current):
      nonlocal previous
      affected = {container_id for container_id in current.keys() | previous.keys()
                  if current.get(container_id) != previous.get(container_id)}
      rerouted = {container_id for container_id in affected if {
          state.routable for state in (previous.get(container_id), current.get(container_id)) if state is not None} == {True, False}}
      routability = {container_id for container_id in affected if container_id in previous and container_id in current
                     and previous[container_id].routable != current[container_id].routable}
      previous = current
      for container in Container.select().where(Container.id.in_(affected)):
        container_changed(container)
      self.request_domain_applies(domain.id for domain in Domain.select(Domain.id).where(Domain.container.in_(rerouted)))
      if Domain.select().where(Domain.container.in_(routability) & Domain.service.is_null(False)).exists():
        self.request_discovery()

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
                  scopes={placement_scope(container.worker_id, container.id)}, executor="app",
                  params={"container_id": container.id, "incident": incident, "final": final}, labels=self.container_labels(container))

  def request_reset(self, container: Container, cause: str):
    """Queues the reset of a container whose runtime contradicts its record."""
    self.add_task(task=self.reset_container, key=("reset", container.id),
                  scopes={placement_scope(container.worker_id, container.id)}, executor="app",
                  params={"container_id": container.id, "cause": cause}, labels=self.container_labels(container))

  async def recover_container(self, container_id: str, incident: str, final: bool):
    """Recovers from an operation on the container that never settled, or from its unreadable record, once no task
    owns it: stops the container and keeps the record recovering with the old work's allowance; once that allowance
    has passed (`final`), stops it again and settles the record stopped, failed by the interruption. An operation that
    kept a baseline under this Sage's current authority (an automatic backup) is stopped as it is instead, and at the
    end brought back to that baseline, its last intentional outcome kept; when that fails it is stopped and settled
    failed. Nothing is done when the record no longer shows that incident."""
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
    baseline = operation.baseline if operation is not None and record.authority == authority else None
    resume_failure = None
    if final and baseline is not None:
      try:
        verified_at = await self.resume_runtime(container.worker_id, container.id, baseline)
      except Exception as error:
        resume_failure = f"it could not be started again as it was before the backup: {error}"
      else:
        await Containers().write(container, replace(record, operation=replace(
            operation, phase="settled", outcome=operation.outcome or "interrupted", finished_at=utcnow(),
            verified_on_worker_at=verified_at, recovery=Recovery("verified"))))
        record_container_event(container, contract, "recovered", operation.id, dedupe_key=f"{operation.id}:recovered")
        await mark(container, "Back as it was before an interrupted backup")
        return
    try:
      if baseline is not None and not final:
        await self.quiesce_runtime(container.worker_id, container.id)
      else:
        verified_at = await self.stop_runtime(container.worker_id, container.id)
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
    reason = resume_failure or operation.reason or f"its {NAMES[contract].lower()} was interrupted"
    kept = record if record is not None and record.authority == authority else None
    await Containers().write(container, TargetRecord(
        authority, kept.applied if kept else None, Settled(operation.id, "stopped", "failed", now, reason),
        replace(operation, phase="settled", outcome=operation.outcome or "interrupted", finished_at=now,
                verified_on_worker_at=verified_at, recovery=Recovery("verified"))))
    record_container_event(container, contract, "recovered", operation.id, dedupe_key=f"{operation.id}:recovered")
    await mark(container, "Stopped and settled after an interrupted operation")
    name, page = self.container_notice(container)
    Notifications().notify(f"{name} was stopped after an interrupted {NAMES[contract].lower()}.", "warning", page)

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
    reading, _ = await self.inspect_runtime(container.worker_id, container.id)
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
      verified_at = await self.stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "failed", now, RESETS[found.cause]),
          operation=replace(operation, phase="settled", outcome="succeeded", reason=found.cause, finished_at=now,
                            verified_on_worker_at=verified_at)))
    except Exception as error:
      await self.fail_lifecycle(container, record, error)
    record_container_event(container, "reset", "succeeded", operation.id, reason={"cause": found.cause},
                           dedupe_key=f"{operation.id}:succeeded")
    await mark(container, f"Stopped: {RESETS[found.cause]}")
    name, page = self.container_notice(container)
    Notifications().notify(f"{name} was stopped: {RESETS[found.cause]}.", "warning", page)

  def request_record_read(self, container: Container):
    """Queues a read of the container's record from its worker, under its scope so it never crosses a write."""
    self.add_task(
        task=self.read_record, key=("read_record", container.id), scopes={placement_scope(container.worker_id, container.id)},
        executor="app", params={"container_id": container.id}, labels=self.container_labels(container), quiet=True)

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
                  scopes={placement_scope(hostname, container_id)}, executor="app",
                  params={"hostname": hostname, "container_id": container_id},
                  labels={"Worker": hostname, "Container": container_id, **({"Application": application} if application else {})})

  async def cleanup_orphan(self, hostname: str, container_id: str):
    """Removes what this Sage left on the worker for a container it has no row for: its runtime and its placement,
    volume data included. Nothing is done while the row is there, when its record or runtime is another Sage's, or
    when nothing of it is left (what asked for it saw the worker before the container's Delete ended); an operation its
    record shows unsettled (any, when the record is unreadable) is first waited out from when Sage saw it."""
    if Container.select().where((Container.id == container_id) & (Container.worker == hostname)).exists():
      logger.info(f"Container {container_id} is on {hostname} again; nothing to clean up.")
      return
    instance_id = Instance.get().id
    _, lines = await Tailscale().exec_command(hostname, records_command(config.WORKER_HOME_DIR, container_id), timeout=30)
    target = parse_record_listing(lines).get(container_id, TargetRead("absent"))
    reading, _ = await self.inspect_runtime(hostname, container_id)
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
    left = target.kind != "absent" or reading.state != "absent"
    directory = placement_dir(config.WORKER_HOME_DIR, container_id)
    try:
      if left:
        await self.stop_runtime(hostname, container_id, "--volumes --rmi local --remove-orphans")
      _, removed = await Tailscale().exec_command(
          hostname, f"if [ -d {directory} ]; then rm -rf {directory}; echo removed; fi", timeout=30)
    except Exception as error:
      logger.error(f"The orphan {container_id} on {hostname} could not be removed: {error}")
      raise TaskFailed()
    self.orphans.pop((hostname, container_id), None)
    if not left and not removed:
      logger.info(f"Nothing of container {container_id} is left on {hostname}; nothing to clean up.")
      return
    Notifications().notify(f"Removed a container Sage no longer has from worker {hostname}.", "warning", f"/workers/{hostname}")

  def _container_tasks(self) -> tuple[dict[str, str], dict[str, tuple[QueuedRequest, ...]]]:
    """By container id, from one look at the queue: the operation running on each container that shows its progress,
    and the requests of that kind queued on it in their turn, each cancellable when it is the user's request that
    `request_cancel` takes."""
    snapshot = self.task_snapshot()
    live = {task["key"][1]: task["name"] for task in snapshot["running"] if task["name"] in PROGRESS}
    queued = {}
    for task in snapshot["queued"]:
      if task["name"] in PROGRESS:
        container_id = task["key"][1]
        cancellable = task["name"] in REQUESTS and task["key"] == [REQUESTS[task["name"]][0], container_id]
        queued[container_id] = (*queued.get(container_id, ()), QueuedRequest(task["name"], task["task_id"], cancellable))
    return live, queued

  def request_deploy(self, container_id: str) -> str:
    """Queues a Deploy of the container with its application's inputs as they are now; the task's id."""
    container = self.lifecycle_container(container_id)
    deploy = capture_deploy_config(container.application_id)
    deploy.check_deployable()
    return self.admitted_task_id(self.queue_lifecycle(container, "deploy", self.deploy_container, {"deploy": deploy}))

  def request_stop(self, container_id: str) -> str:
    """Queues a Stop of the container; the task's id."""
    return self.admitted_task_id(self.queue_lifecycle(self.lifecycle_container(container_id), "stop", self.stop_container, {}))

  def request_deploy_all(self, application_id: int) -> list[str]:
    """Queues a Deploy of each of the application's containers, all with its inputs as they are now; the ids of the
    tasks queued."""
    containers = self.application_containers(application_id)
    deploy = capture_deploy_config(application_id)
    deploy.check_deployable()
    added = [self.queue_lifecycle(container, "deploy", self.deploy_container, {"deploy": deploy})
             for container in containers]
    return [result.task_id for result in added if result.outcome == Outcome.ACCEPTED]

  def request_stop_all(self, application_id: int) -> list[str]:
    """Queues a Stop of each of the application's containers; the ids of the tasks queued."""
    added = [self.queue_lifecycle(container, "stop", self.stop_container, {})
             for container in self.application_containers(application_id)]
    return [result.task_id for result in added if result.outcome == Outcome.ACCEPTED]

  def request_delete(self, container_id: str, force: bool = False) -> str:
    """Queues a Delete of the container, after cancelling its queued tasks; with `force`, a worker that can't be
    reached is left as it is. The task's id."""
    return self.admitted_task_id(self.queue_lifecycle(
        self.lifecycle_container(container_id), "delete", self.delete_container, {}, {"force": force}))

  def request_cancel(self, container_id: str, operation: str) -> str:
    """Cancels the container's queued `operation` (one of `REQUESTS`: deploy, stop, a manual backup, a volume restore or
    delete); the cancelled task's id."""
    if operation not in REQUESTS:
      raise ValueError(f"Unknown operation {operation!r}.")
    key = (REQUESTS[operation][0], container_id)
    cancelled = self.cancel_tasks(key, operation)
    if cancelled:
      return cancelled[0]
    if self.has_task(key, running_only=True):
      raise Conflict(f"The {operation} of this container has already started.")
    raise LookupError(f"This container has no queued {operation}.")

  async def deploy_container(self, container_id: str, deploy: DeployConfig):
    """Stops the container, writes the deployment's files, starts it and verifies it runs this deployment."""
    begun = await self.begin_lifecycle(container_id, "deploy", deploy.content_hash)
    if begun is None:
      return
    container, record = begun
    operation, host, home = record.operation, container.worker_id, config.WORKER_HOME_DIR
    directory = placement_dir(home, container.id)
    try:
      await Containers().write(container, record)
      await self.stop_runtime(container.worker_id, container.id)

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
      reading, verified_at = await self.inspect_runtime(container.worker_id, container.id)
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
      await self.fail_lifecycle(container, record, error)
    record_container_event(container, "deploy", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Deployed")
    self.request_domain_applies(domain.id for domain in container.domains)
    name, page = self.container_notice(container)
    Notifications().notify(f"{name} deployed.", "success", page)

  async def stop_container(self, container_id: str):
    """Stops the container and verifies nothing of it runs."""
    begun = await self.begin_lifecycle(container_id, "stop")
    if begun is None:
      return
    container, record = begun
    operation = record.operation
    try:
      await Containers().write(container, record)
      verified_at = await self.stop_runtime(container.worker_id, container.id)
      now = utcnow()
      await Containers().write(container, replace(
          record, settled=Settled(operation.id, "stopped", "succeeded", now),
          operation=replace(operation, phase="settled", outcome="succeeded", finished_at=now,
                            verified_on_worker_at=verified_at)))
    except Exception as error:
      await self.fail_lifecycle(container, record, error)
    record_container_event(container, "stop", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
    await mark(container, "Stopped")
    self.request_domain_applies(domain.id for domain in container.domains)
    name, page = self.container_notice(container)
    Notifications().notify(f"{name} stopped.", "success", page)

  async def delete_container(self, container_id: str, force: bool = False):
    """Stops the container and removes its placement from the worker, then its row. With `force`, a worker that can't
    be reached is left as it is and only the row goes."""
    begun = await self.begin_lifecycle(container_id, "delete", force=force)
    if begun is None:
      return
    container, record = begun
    name, page = self.container_notice(container)
    if record is None:
      record_container_event(container, "delete", "succeeded", reason={"worker": "not cleaned up"})
      await mark(container, "Removed from Sage; the worker was not cleaned up")
    else:
      operation = record.operation
      try:
        await Containers().write(container, record)
        await self.stop_runtime(container.worker_id, container.id, "--volumes --rmi local --remove-orphans")
        await Tailscale().exec_command(
            container.worker_id, f"rm -rf {placement_dir(config.WORKER_HOME_DIR, container.id)}", timeout=30)
      except Exception as error:
        await self.fail_lifecycle(container, record, error)
      record_container_event(container, "delete", "succeeded", operation.id, dedupe_key=f"{operation.id}:succeeded")
      await mark(container, "Removed from this worker")
    domains = detach_container(container.id)
    Containers().forget(container.id)
    self.request_domain_applies(domains)
    if domains:
      self.request_domain_reconciliation()
    if record is None:
      Notifications().notify(f"{name} deleted from Sage; the worker could not be reached, so its files stay there.",
                             "warning", page)
    else:
      Notifications().notify(f"{name} deleted.", "success", page)
    logger.info(f"Container {container.id} deleted; {len(domains)} domains unlinked.")
