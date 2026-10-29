import asyncio
import json
import logging
import re
import shlex

from services.db import APPLICATION_BUSY_STATUSES, Application, Container, DeployConfig, Event, Project, Worker, db
from services.worker_reports import WorkerReports
from utils.common import format_yaml, parse_multiline_kv, utcnow
from utils.logging import generate_task_id_token, task_id
from utils.queue import OnConflict

from ._common import app_dir
from .decisions import ABSENT, OFFLINE, SEEN, UNKNOWN, Observation, reconcile_container, rollup_status

logger = logging.getLogger(__name__)

# What is said when an application's status follows from its containers': the end of the sentence
# that starts with the application's name, and the notification level.
_ROLLUP_NOTICES = {
    "error": ("is in error state as at least one container is in error state.", "error"),
    "active": ("is active as all containers are active.", "success"),
    "inactive": ("is inactive as all containers are inactive.", "warning"),
}


class ApplicationMixin:
  async def deploy_application(self, application_id: int):
    """
    Deploy an application.
    """
    # One transaction freezes the deploy inputs (app config, project env,
    # volumes) and the container set at a single point in time; direct UI edits
    # made after this apply on the next deploy. deployed_at is set before the
    # snapshot so config.deploy_stamp is this deploy's version marker.
    with db.atomic():
      application = Application.get_or_none(Application.id == application_id)
      if application is None:
        logger.info(f"Application {application_id} deleted before deploy; nothing to do.")
        return

      containers = list(application.containers)
      if not containers:
        was_inactive = application.status == "inactive"
        application_qualified_name = application.qualified_name
        if not was_inactive:
          application.status = "inactive"
          application.save()
      else:
        application.status = "deploying"
        application.deployed_at = utcnow()
        config = application.deploy_config
        application.deployed_hash = config.content_hash
        application.save()

    if not containers:
      # A last-container delete can land before a queued deploy runs; deploying
      # over an empty set would fake a successful "active". Routing was already
      # dropped by whatever removed the containers (delete_container syncs when
      # it removes an active one; the reconcile scan backstops the rest).
      if not was_inactive:
        self.notify(
            f"Application {application_qualified_name} has no containers to deploy; marked inactive.",
            "warning")
      return

    logger.info(f"Deploying application {config.qualified_name}...")

    await asyncio.gather(
        *[self.deploy_application_container(container, config)
          for container in containers],
        return_exceptions=False,
    )

    # Trigger traefik sync
    self.request_application_traefik_sync(application)

    # Check only the snapshot's container set: an instant create mid-deploy adds
    # an inactive row the minutely status sync owns, not this deploy's outcome.
    if any(container.status == "error" for container in containers):
      application.status = "error"
      application.save()
      self.notify(
          f"Failed to deploy application {config.qualified_name}.",
          "error")
      raise Exception(f"Failed to deploy application {config.qualified_name}.")
    else:
      application.status = "active"
      application.save()
      self.notify(f"Application {config.qualified_name} deployed.", "success")

  async def deploy_application_container(self, container: Container, config: DeployConfig):
    # Create an event for tracking with a different task id.
    container_task_id = generate_task_id_token()
    exception_message = None
    task_id_token = None

    try:
      Event.create(
          container=container,
          type="deploy",
          application_task_id=task_id.get(),
          container_task_id=container_task_id,
      )
      container.status = "deploying"
      container.save()
      container_dir = f"{self.worker_home_dir}/applications/{config.qualified_name}"
      logger.info(
          f"Deploying application {config.qualified_name} container to worker {
              container.worker.hostname} with task id {container_task_id}...")
      task_id_token = task_id.set(container_task_id)

      if not self.is_worker_online(container.worker.hostname):
        raise Exception(f"Worker {container.worker.hostname} is offline.")

      project_env = parse_multiline_kv(config.project_env, lambda key, value: (key, value))

      # Special SAGE specific variables
      project_env.append(("SAGE_WORKER_HOSTNAME", container.worker.hostname))

      app_env = config.env
      app_build_args = config.args
      app_build_secrets = config.build_secrets
      app_command = config.command

      # Resolve Application env, build args, build secrets and command with project env values if they reference them with ${KEY}
      for key, value in project_env:
        app_env = app_env.replace("${" + key + "}", str(value))
        app_build_args = app_build_args.replace("${" + key + "}", str(value))
        app_build_secrets = app_build_secrets.replace("${" + key + "}", str(value))
        app_command = app_command.replace("${" + key + "}", str(value))

      app_build_args = parse_multiline_kv(app_build_args, lambda key, value: json.dumps(f"{key}={value}"),
                                          strip_quotes=True)
      build_secret_items = parse_multiline_kv(app_build_secrets, lambda key, value: (key, value),
                                              strip_quotes=True)

      # Override the image command in exec/array form so tokens pass through literally and YAML stays safe.
      # Left blank when unset so the template renders `command:` (null), which Compose drops in favour
      # of the image's default CMD. An empty string ("") would override CMD to empty, so avoid it.
      command_value = ""
      if app_command.strip():
        command_tokens = ", ".join(json.dumps(token) for token in shlex.split(app_command))
        command_value = f"[{command_tokens}]"

      # Create the secrets file
      await self.tailscale.sync_file(
          container.worker.hostname,
          app_dir / "templates/worker/file",
          f"{container_dir}/.env",
          {"CONTENT": app_env},
          mode=0o600,
      )

      # Create the volumes
      volumes = config.volumes
      volumes_config = [
          json.dumps(f"{container_dir}/volumes/{name}:{path}")
          for name, path in volumes
      ]
      volume_mkdir_cmd = ';'.join([f"mkdir -p {container_dir}/volumes"] + [f"mkdir -p {container_dir}/volumes/{name}" for name, _ in volumes])
      await self.tailscale.exec_command(
          container.worker.hostname,
          volume_mkdir_cmd
      )

      # Get existing volumes on worker which are not in the current config and need to be cleaned up
      _, existing_volumes = await self.tailscale.exec_command(
          container.worker.hostname,
          f"ls -1 {container_dir}/volumes || true",
      )
      # Restrict cleanup to Sage's own volume naming so a directory planted on the
      # worker can't smuggle shell metacharacters into the remote rm command.
      volumes_to_cleanup = {
          name for name in (set(existing_volumes) - set(vname for vname, _ in volumes))
          if re.fullmatch(r"[a-z0-9-]+", name)
      }
      if volumes_to_cleanup:
        volume_cleanup_cmd = ';'.join([f"rm -rf {container_dir}/volumes/{v}" for v in volumes_to_cleanup])
        await self.tailscale.exec_command(
            container.worker.hostname,
            volume_cleanup_cmd
        )

      # Create the compose file based on application type
      if config.type == "docker":
        await self.tailscale.sync_file(
            container.worker.hostname,
            app_dir / "templates/worker/application/dockerhub-compose.yml",
            f"{container_dir}/docker-compose.yml",
            {
                "CONTAINER_NAME": config.qualified_name,
                "DEPLOYED_AT": config.deploy_stamp,
                "IMAGE": config.image,
                "COMMAND": command_value,
                "VOLUMES": ", ".join(volumes_config),
            },
            formatter=format_yaml,
        )
      elif config.type == "git":
        # Each build secret is backed by its own file under secrets/
        secrets_dir = f"{container_dir}/secrets"
        await self.tailscale.exec_command(
            container.worker.hostname,
            f"rm -rf {secrets_dir}" + (f" && mkdir -m 700 {secrets_dir}" if build_secret_items else ""),
        )
        for name, value in build_secret_items:
          await self.tailscale.sync_file(
              container.worker.hostname,
              app_dir / "templates/worker/file",
              f"{secrets_dir}/{name}",
              {"CONTENT": value},
              mode=0o600,
          )

        secrets_block = "secrets:\n" + "\n".join(
            f"  {name}:\n    file: ./secrets/{name}" for name, _ in build_secret_items
        ) if build_secret_items else ""

        await self.tailscale.sync_file(
            container.worker.hostname,
            app_dir / "templates/worker/application/gitrepo-compose.yml",
            f"{container_dir}/docker-compose.yml",
            {
                "CONTAINER_NAME": config.qualified_name,
                "DEPLOYED_AT": config.deploy_stamp,
                "REPO": config.repo,
                "DOCKERFILE": config.path,
                "BUILD_ARGS": ", ".join(app_build_args),
                "BUILD_SECRETS": ", ".join(name for name, _ in build_secret_items),
                "SECRETS_BLOCK": secrets_block,
                "COMMAND": command_value,
                "VOLUMES": ", ".join(volumes_config),
            },
            formatter=format_yaml,
        )

      # Deploy with docker compose. 900s: a git build + image pull + --wait
      # healthcheck can take well past the 300s default.
      await self.tailscale.exec_command(
          container.worker.hostname,
          f"docker compose -f {container_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --build",
          timeout=900,
      )

      deployment_status = "active"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      if task_id_token is not None:
        task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "active":
      self.notify(f"Application {config.qualified_name} container deployed to worker {container.worker.hostname}.")
    else:
      self.notify(
          f"Failed to deploy application {
              config.qualified_name} container to worker {
              container.worker.hostname}: {exception_message}", "error")

  async def delete_container(self, container_id: int, force: bool = False):
    """
    Delete a container.
    """
    container = Container.get_or_none(Container.id == container_id)
    if container is None:
      logger.info(f"Container {container_id} already deleted; nothing to do.")
      return
    was_active = container.status == "active"
    # Create an event for tracking in case of error.
    Event.create(
        container=container,
        type="delete",
        application_task_id=task_id.get(),
        container_task_id=task_id.get(),
    )
    container.status = "stopping"
    container.save()

    container_dir = f"{self.worker_home_dir}/applications/{container.application.qualified_name}"

    logger.info(
        f"Deleting container of application {container.application.qualified_name} from worker {
            container.worker.hostname}")

    try:
      skip_remote_cleanup = force and not self.is_worker_online(container.worker.hostname)

      if skip_remote_cleanup:
        logger.warning(
            f"Force deleting container of application {container.application.qualified_name} from offline worker {
                container.worker.hostname}; skipping remote cleanup.")
      elif not self.is_worker_online(container.worker.hostname):
        raise Exception(
            f"Worker {container.worker.hostname} is offline; use force delete to remove the container record anyway.")
      else:
        # Stop container on worker
        await self.tailscale.exec_command(
            container.worker.hostname,
            f'[ ! -d "{container_dir}" ] || docker compose -f "{container_dir}/docker-compose.yml" down --volumes --rmi all --remove-orphans',
            timeout=60,
        )

        # Remove application folder
        await self.tailscale.exec_command(
            container.worker.hostname, f"rm -rf {container_dir}", timeout=30
        )

        # Remove traefik config
        await self.tailscale.exec_command(
            container.worker.hostname,
            f"rm -rf {self.worker_home_dir}/traefik/dynamic/{container.application.qualified_name}-*.yml",
            timeout=30,
        )

      # Delete database record
      application = container.application
      container.delete_instance()
      # Only a container that was in the routing pool needs a resync to drop it.
      if was_active:
        self.request_application_traefik_sync(application)

      if skip_remote_cleanup:
        self.notify(
            f"Container of application {container.application.qualified_name} force-deleted from offline worker {
                container.worker.hostname}. Remote cleanup skipped.",
            "warning",
        )
      else:
        self.notify(
            f"Container of application {container.application.qualified_name} deleted from worker {
                container.worker.hostname}.", "success")
    except Exception as e:
      container.status = "error"
      container.save()
      self.notify(
          f"Failed to delete container of application {container.application.qualified_name} from worker {
              container.worker.hostname}: {e}", "error")
      raise Exception(
          f"Failed to delete container {
              container.id} of application {container.application.qualified_name} from worker {
              container.worker.hostname}: {e}")

  async def stop_application(self, application_id: int):
    """
    Stop an application.
    """
    application = Application.get_by_id(application_id)
    application.status = "stopping"
    application.save()
    logger.info(f"Stopping application {application.qualified_name}...")

    await asyncio.gather(
        *[self.stop_application_container(container) for container in application.containers],
        return_exceptions=False,
    )

    # Containers no longer active -> refresh Traefik routing.
    self.request_application_traefik_sync(application)

    if any(container.status == "error" for container in application.containers):
      application.status = "error"
      application.save()
      self.notify(f"Failed to stop application {application.qualified_name}.", "error")
      raise Exception(f"Failed to stop application {application.qualified_name}.")
    else:
      application.status = "inactive"
      application.save()
      self.notify(f"Application {application.qualified_name} stopped.", "success")

  async def stop_application_container(self, container: Container):
    # Create an event for tracking with a different task id.
    container_task_id = generate_task_id_token()
    was_inactive = container.status == "inactive"
    exception_message = None
    task_id_token = None

    try:
      Event.create(
          container=container,
          type="stop",
          application_task_id=task_id.get(),
          container_task_id=container_task_id,
      )
      container.status = "stopping"
      container.save()
      logger.info(
          f"Stopping application {container.application.qualified_name} container on worker {
              container.worker.hostname} with task id {container_task_id}...")
      task_id_token = task_id.set(container_task_id)
      container_dir = f"{self.worker_home_dir}/applications/{container.application.qualified_name}"

      if was_inactive:
        logger.info(
            f"Container of application {container.application.qualified_name} on worker {
                container.worker.hostname} is already inactive. Skipping compose down.")
      elif not self.is_worker_online(container.worker.hostname):
        raise Exception(f"Worker {container.worker.hostname} is offline.")
      else:
        # Stop with docker compose
        await self.tailscale.exec_command(
            container.worker.hostname,
            f"docker compose -f {container_dir}/docker-compose.yml down",
        )

      deployment_status = "inactive"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      if task_id_token is not None:
        task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "inactive":
      self.notify(
          f"Application {container.application.qualified_name} container stopped on worker {container.worker.hostname}.")
    else:
      self.notify(
          f"Failed to stop application {
              container.application.qualified_name} container on worker {
              container.worker.hostname}: {exception_message}",
          "error")

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
      application.save()

    containers = list(application.containers)
    if not containers:
      # No containers left: the app has nothing to run, so it's inactive. Catches
      # any app left non-inactive with a zero container count.
      if application.status != "inactive":
        application.status = "inactive"
        application.save()
        self.request_application_traefik_sync(application)
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
        container.save()
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
        except Exception as e:
          status = "error"
          message = f"Application container {worker_container_name} is running a stale version and could not be stopped: {e}"
          level = "error"
      if status is not None and status != container.status:
        container.status = status
        container.save()
        routing_changed = True
      if message:
        self.notify(message, level)

    if routing_changed:
      self.request_application_traefik_sync(application)

    # Sync the overall application status from the (possibly updated) containers.
    status = rollup_status(container.status for container in application.containers)
    if status is not None and status != application.status:
      application.status = status
      application.save()
      notice, level = _ROLLUP_NOTICES[status]
      self.notify(f"Application {application.qualified_name} {notice}", level)

  def observe_application(self, application, worker, last_write) -> Observation:
    """How the application's container stands on a worker, from the worker's report. A worker out of
    the pool is OFFLINE. With no report that can be compared with rows last written at `last_write`
    (`WorkerReports.trusted`) nothing is known: the containers keep their status until the next report or the
    next minute's sync. A container with no label has an empty version, which matches no deploy stamp."""
    if not self.is_worker_online(worker.hostname):
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
