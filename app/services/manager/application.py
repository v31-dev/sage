import asyncio
import json
import logging
import re
import shlex

from services.db import (
    APPLICATION_BUSY_STATUSES,
    Application,
    Container,
    Event,
)
from utils.common import get_env, parse_multiline_kv
from utils.executor import run_in_executor_with_context
from utils.logging import generate_task_id_token, task_id

from ._common import app_dir

logger = logging.getLogger(__name__)


class ApplicationMixin:
  async def deploy_application(self, application_id: int):
    """
    Deploy an application.
    """
    application = Application.get_by_id(application_id)
    application.status = "deploying"
    application.save()
    logger.info(f"Deploying application {application.qualified_name}...")

    await asyncio.gather(
        *[self.deploy_application_container(container)
          for container in application.containers],
        return_exceptions=False,
    )

    if any(container.status == "error" for container in application.containers):
      application.status = "error"
      application.save()
      self.notify(
          f"Failed to deploy application {application.qualified_name}.",
          "error")
      raise Exception(f"Failed to deploy application {application.qualified_name}.")
    else:
      application.status = "active"
      application.save()
      self.notify(f"Application {application.qualified_name} deployed.", "success")

  async def deploy_application_container(self, container: Container):
    # Create an event for tracking with a different task id.
    container_task_id = generate_task_id_token()
    Event.create(
        container=container,
        type="deploy",
        application_task_id=task_id.get(),
        container_task_id=container_task_id,
    )
    container.status = "deploying"
    container.save()
    container_dir = f"{self.worker_home_dir}/applications/{container.application.qualified_name}"
    logger.info(
        f"Deploying application {container.application.qualified_name} container to worker {
            container.worker.hostname} with task id {container_task_id}...")

    exception_message = None

    try:
      task_id_token = task_id.set(container_task_id)

      project_env = container.application.project.env if container.application.project.env else ""
      project_env = parse_multiline_kv(project_env, lambda key, value: (key, value))

      # Special SAGE specific variables
      project_env.append(("SAGE_WORKER_HOSTNAME", container.worker.hostname))

      app_env = container.application.env if container.application.env else ""
      app_build_args = container.application.args if container.application.args else ""
      app_command = container.application.command if container.application.command else ""

      # Resolve Application env, build args and command with project env values if they reference them with ${KEY}
      for key, value in project_env:
        app_env = app_env.replace("${" + key + "}", str(value))
        app_build_args = app_build_args.replace("${" + key + "}", str(value))
        app_command = app_command.replace("${" + key + "}", str(value))

      app_build_args = parse_multiline_kv(app_build_args, lambda key, value: json.dumps(f"{key}={value}"),
                                          strip_quotes=True)

      # Override the image command in exec/array form so tokens pass through literally and YAML stays safe.
      # Left blank when unset so the template renders `command:` (null), which Compose drops in favour
      # of the image's default CMD. An empty string ("") would override CMD to empty, so avoid it.
      command_value = ""
      if app_command.strip():
        command_tokens = ", ".join(json.dumps(token) for token in shlex.split(app_command))
        command_value = f"[{command_tokens}]"

      # Create the secrets file
      await run_in_executor_with_context(
          self.tailscale.sync_file,
          container.worker.hostname,
          app_dir / "templates/worker/file",
          f"{container_dir}/.env",
          {"CONTENT": app_env},
      )

      # Create the volumes
      volumes = list(container.application.volumes)
      volumes_config = [
          json.dumps(f"{container_dir}/volumes/{v.name}:{v.path}")
          for v in volumes
      ]
      volume_mkdir_cmd = ';'.join([f"mkdir -p {container_dir}/volumes"] + [f"mkdir -p {container_dir}/volumes/{v.name}" for v in volumes])
      await run_in_executor_with_context(
          self.tailscale.exec_command,
          container.worker.hostname,
          volume_mkdir_cmd
      )

      # Get existing volumes on worker which are not in the current config and need to be cleaned up
      _, existing_volumes = await run_in_executor_with_context(
          self.tailscale.exec_command,
          container.worker.hostname,
          f"ls -1 {container_dir}/volumes || true",
      )
      # Restrict cleanup to Sage's own volume naming so a directory planted on the
      # worker can't smuggle shell metacharacters into the remote rm command.
      volumes_to_cleanup = {
          name for name in (set(existing_volumes) - set(v.name for v in volumes))
          if re.fullmatch(r"[a-z0-9-]+", name)
      }
      if volumes_to_cleanup:
        volume_cleanup_cmd = ';'.join([f"rm -rf {container_dir}/volumes/{v}" for v in volumes_to_cleanup])
        await run_in_executor_with_context(
            self.tailscale.exec_command,
            container.worker.hostname,
            volume_cleanup_cmd
        )

      # Create the compose file based on application type
      if container.application.type == "docker":
        await run_in_executor_with_context(
            self.tailscale.sync_file,
            container.worker.hostname,
            app_dir / "templates/worker/application/dockerhub-compose.yml",
            f"{container_dir}/docker-compose.yml",
            {
                "APPLICATION_NAME": container.application.name,
                "CONTAINER_NAME": container.application.qualified_name,
                "IMAGE": container.application.image,
                "COMMAND": command_value,
                "VOLUMES": ", ".join(volumes_config),
            },
        )
      elif container.application.type == "git":
        await run_in_executor_with_context(
            self.tailscale.sync_file,
            container.worker.hostname,
            app_dir / "templates/worker/application/gitrepo-compose.yml",
            f"{container_dir}/docker-compose.yml",
            {
                "APPLICATION_NAME": container.application.name,
                "CONTAINER_NAME": container.application.qualified_name,
                "REPO": container.application.repo,
                "DOCKERFILE": container.application.path,
                "BUILD_ARGS": ", ".join(app_build_args),
                "COMMAND": command_value,
                "VOLUMES": ", ".join(volumes_config),
            },
        )

      # Deploy with docker compose
      await run_in_executor_with_context(
          self.tailscale.exec_command,
          container.worker.hostname,
          f"docker compose -f {container_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --build",
      )

      deployment_status = "active"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "active":
      self.notify(f"Application {container.application.qualified_name} container deployed to worker {container.worker.hostname}.")
    else:
      self.notify(
          f"Failed to deploy application {
              container.application.qualified_name} container to worker {
              container.worker.hostname}: {exception_message}", "error")

  def delete_container(self, container_id: int, force: bool = False):
    """
    Delete a container.
    """
    container = Container.get_by_id(container_id)
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
      skip_remote_cleanup = force and not container.worker.online

      if skip_remote_cleanup:
        logger.warning(
            f"Force deleting container of application {container.application.qualified_name} from offline worker {
                container.worker.hostname}; skipping remote cleanup.")
      else:
        # Stop container on worker
        self.tailscale.exec_command(
            container.worker.hostname,
            f'[ ! -d "{container_dir}" ] || docker compose -f "{container_dir}/docker-compose.yml" down --volumes --rmi all --remove-orphans',
            timeout=60,
        )

        # Remove application folder
        self.tailscale.exec_command(
            container.worker.hostname, f"rm -rf {container_dir}", timeout=30
        )

        # Remove traefik config
        self.tailscale.exec_command(
            container.worker.hostname,
            f"rm -rf {self.worker_home_dir}/traefik/dynamic/{container.application.qualified_name}-*.yml",
            timeout=30,
        )

      # Delete database record
      container.delete_instance()

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

    exception_message = None

    try:
      task_id_token = task_id.set(container_task_id)
      container_dir = f"{self.worker_home_dir}/applications/{container.application.qualified_name}"

      if was_inactive:
        logger.info(
            f"Container of application {container.application.qualified_name} on worker {
                container.worker.hostname} is already inactive. Skipping compose down.")
      else:
        # Stop with docker compose
        await run_in_executor_with_context(
            self.tailscale.exec_command,
            container.worker.hostname,
            f"docker compose -f {container_dir}/docker-compose.yml down",
        )

      deployment_status = "inactive"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
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

  def sync_application_status(self, application_id: int):
    """
    Sync a single application's container & overall status from its workers.
    Ideally status is managed explicitly; this catches unexpected changes like
    a container stopped from the worker side or a worker going offline without
    the Manager knowing yet.
    """
    application = Application.get_by_id(application_id)

    # The queue scope keeps this off an app with an operation running, but guard
    # defensively against any explicit-action status.
    if application.status in APPLICATION_BUSY_STATUSES:
      return

    containers = list(application.containers)
    if not containers:
      return

    # Query container state only on the (distinct, online) workers backing this
    # application's containers.
    container_status = {}
    workers = {container.worker.hostname: container.worker for container in containers}
    for hostname, worker in workers.items():
      if not worker.online:
        continue
      try:
        _, docker_ps_output = self.tailscale.exec_command(
            hostname, "docker ps --format '{{.Names}}|{{.State}}'")
        for line in docker_ps_output:
          try:
            container_name, container_state = line.split("|")
          except Exception:
            continue
          container_status[f"{hostname}-{container_name}"] = container_state
      except Exception as e:
        logger.error(
            f"Failed to get container status from worker {hostname} while syncing application {application.qualified_name}: {e}")

    for container in containers:
      # Skip state update during explicit actions
      if container.status in APPLICATION_BUSY_STATUSES:
        continue

      worker_container_name = f"{container.worker.hostname}-{application.qualified_name}"
      status = container_status.get(worker_container_name)
      if status:
        if status == "running" and container.status != "active":
          container.status = "active"
          container.save()
          self.notify(f"Application container {worker_container_name} is active again.", "success")
        elif status in ["paused", "restarting"] and container.status != "error":
          container.status = "error"
          container.save()
          self.notify(f"Application container {worker_container_name} is in error state ({status}).", "error")
      else:
        # if no status is found and container is supposed to be active, mark as
        # error (could be offline or stopped container)
        if container.status == "active":
          container.status = "error"
          container.save()
          self.notify(f"Application container {worker_container_name} is in error state (status not found).", "error")

    # Sync the overall application status from the (possibly updated) containers.
    if application.status in APPLICATION_BUSY_STATUSES:
      return

    containers = list(application.containers)

    if any(c.status == "error" for c in containers):
      if application.status != "error":
        application.status = "error"
        application.save()
        self.notify(
            f"Application {application.qualified_name} is in error state as at least one container is in error state.",
            "error")

    elif all(c.status == "active" for c in containers):
      if application.status != "active":
        application.status = "active"
        application.save()
        self.notify(
            f"Application {application.qualified_name} is active as all containers are active.",
            "success")

    elif all(c.status == "inactive" for c in containers):
      if application.status != "inactive":
        application.status = "inactive"
        application.save()
        self.notify(
            f"Application {application.qualified_name} is inactive as all containers are inactive.",
            "warning")
