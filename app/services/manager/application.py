import logging
from contextlib import contextmanager

from services.applications.updates import application_changed, container_changed
from services.db import APPLICATION_BUSY_STATUSES, Application, Container, Domain, Event, Project, Worker
from services.worker_reports import WorkerReports
from utils.logging import new_task_id, sub_task_id, task_id
from utils.queue import OnConflict

from .containers import mark
from .decisions import ABSENT, OFFLINE, SEEN, UNKNOWN, Observation, reconcile_container, rollup_status

logger = logging.getLogger(__name__)

# What is said when an application's status follows from its containers': the end of the sentence
# that starts with the application's name, and the notification level.
_ROLLUP_NOTICES = {
    "error": ("is in error state as at least one container is in error state.", "error"),
    "active": ("is active as all containers are active.", "success"),
    "inactive": ("is inactive as all containers are inactive.", "warning"),
}


def save_application(application: Application):
  application.save()
  application_changed(application)


@contextmanager
def container_part(container: Container, kind: str):
  """One container's part of the running task (its deploy, stop, delete, backup or restore): records its `Event`, and
  the lines logged inside carry the part's id, which it yields."""
  part = new_task_id()
  Event.create(container=container, type=kind, application_task_id=task_id.get(), container_task_id=part)
  token = sub_task_id.set(part)
  try:
    yield part
  finally:
    sub_task_id.reset(token)


class ApplicationMixin:
  def save_container(self, container: Container):
    was_active = Container.select(Container.status).where(Container.id == container.id).scalar() == "active"
    container.save()
    container_changed(container)
    if was_active != (container.status == "active"):
      self.request_discovery_sync()

  async def sync_application_status(self, application_id: int):
    """
    Sync a single application's container & overall status from its workers.
    Ideally status is managed explicitly; this catches unexpected changes like
    a container stopped from the worker side, a worker going offline without
    the Manager knowing yet, or an operation interrupted before its terminal
    status write (crash, restart).
    """
    application = Application.get_or_none(Application.id == application_id)
    if application is None:
      logger.info(f"Application {application_id} deleted before status sync; nothing to do.")
      return

    # Every busy-status writer runs under this app's scope, which this task
    # holds right now — so a busy status observed here has no owning operation
    # left (it was interrupted). Reset it and reconcile from worker state below.
    application_was_stuck = application.status in APPLICATION_BUSY_STATUSES
    if application_was_stuck:
      self.notify(
          f"Application {application.qualified_name} was stuck in '{application.status}' with no "
          f"running operation; status reset to error.",
          "error")
      application.status = "error"
      save_application(application)

    containers = list(application.containers)
    if not containers:
      # No containers left: the app has nothing to run, so it's inactive. Catches
      # any app left non-inactive with a zero container count.
      if application.status != "inactive":
        application.status = "inactive"
        save_application(application)
        self.notify(f"Application {application.qualified_name} is inactive as it has no containers.", "warning")
      return

    # Where each worker's container stands, from its report when that can be compared with the rows
    # (current, and received after their last write).
    last_write = max(application.updated_at, *(container.updated_at for container in containers))
    observations: dict[str, Observation] = {}
    for container in containers:
      if container.worker.hostname not in observations:
        observations[container.worker.hostname] = self.observe_application(
            application, container.worker, last_write)

    # A container flipping active<->error changes the routing pool, so refresh
    # Traefik once at the end if any of them moved.
    routing_changed = False
    for container in containers:
      hostname = container.worker.hostname
      worker_container_name = f"{hostname}-{application.qualified_name}"

      if container.status in APPLICATION_BUSY_STATUSES:
        # Same ownership rule as the application-level reset above; the observation
        # below then converges it to the real container state.
        if application_was_stuck:
          logger.warning(
              f"Container {worker_container_name} reset from stuck '{container.status}' to error.")
        else:
          self.notify(
              f"Application container {worker_container_name} was stuck in '{container.status}' with no "
              f"running operation; status reset to error.",
              "error")
        container.status = "error"
        self.save_container(container)
        routing_changed = True

      decision = reconcile_container(
          worker_container_name, container.status, observations[hostname], application.deploy_stamp)
      status, message, level = decision.status, decision.message, decision.level
      if decision.stop_stale:
        try:
          await self.tailscale.exec_command(
              hostname,
              f"docker compose -f {self.worker_home_dir}/applications/{application.qualified_name}/docker-compose.yml down",
              timeout=60,
          )
          await mark(container, "Stopped: it ran a stale version; deploy to update it")
        except Exception as e:
          status = "error"
          message = f"Application container {worker_container_name} is running a stale version and could not be stopped: {e}"
          level = "error"
          await mark(container, f"Could not stop the stale version: {e}")
      if status is not None and status != container.status:
        container.status = status
        self.save_container(container)
        routing_changed = True
      if message:
        self.notify(message, level)

    if routing_changed:
      self.request_domain_syncs(Domain.select().where(Domain.container.in_([container.id for container in containers])))

    # Sync the overall application status from the (possibly updated) containers.
    status = rollup_status(container.status for container in application.containers)
    if status is not None and status != application.status:
      application.status = status
      save_application(application)
      notice, level = _ROLLUP_NOTICES[status]
      self.notify(f"Application {application.qualified_name} {notice}", level)

  def observe_application(self, application, worker, last_write) -> Observation:
    """How the application's container stands on a worker, from the worker's report. A worker out of
    the pool is OFFLINE; one that no sync has looked at yet is decided from its report. With no report that can be
    compared with rows last written at `last_write` (`WorkerReports.trusted`) nothing is known: the containers keep
    their status until the next report or the next minute's sync. A container with no label has an empty version,
    which matches no deploy stamp."""
    if self.worker_online(worker.hostname) is False:
      return Observation(OFFLINE)
    snapshot = WorkerReports().trusted(worker.hostname, last_write)
    if snapshot is None:
      return Observation(UNKNOWN)
    reading = snapshot.containers.get(application.qualified_name)
    if reading is None:
      return Observation(ABSENT)
    return Observation(SEEN, reading.status, reading.deployed_at or "")

  def request_application_status_sync(self, application):
    """Queue a status sync for the application. REPLACE, not DEDUP: DEDUP also drops a request while
    the same task is running, and a change that arrives after that task read its input would wait
    for the next cron. REPLACE supersedes only a pending duplicate."""
    self.add_task(
        task=self.sync_application_status,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id},
        executor="app",
        on_conflict=OnConflict.REPLACE,
        quiet=True,
    )

  def request_status_sync_for_containers(self, hostname: str, names):
    """Queue a status sync for each application with a container on the worker named in `names`
    (the containers a report changed)."""
    containers = (
        Container.select(Container, Application, Project)
        .join(Application).join(Project)
        .switch(Container).join(Worker)
        .where(Worker.hostname == hostname))
    for container in containers:
      if container.application.qualified_name in names:
        self.request_application_status_sync(container.application)
