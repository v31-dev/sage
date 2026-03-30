import asyncio
import logging
from pathlib import Path

from utils.common import get_env
from utils.logging import task_id, run_in_executor_with_context, generate_task_id_token
from services.base import Base
from services.tailscale import Tailscale
from services.cloudflare import Cloudflare
from services.traefik import Traefik
from services.db import Database, Worker, Application, Deployment, Container
from services.metrics import Metrics


app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)
worker_home_dir = "/opt/sage"

class Manager(Base):
  def __init__(self):
    super().__init__()
    
    try:
      logger.info("Running Manager setup...")

      # Initialize all services
      Database()
      Metrics()
      self.tailscale = Tailscale()
      Traefik()
      self.cloudflare = Cloudflare()
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")
  
  def sync_workers(self):
    '''
      Check for worker changes.
      - New workers (not in db but in tailscale)
      - Offline workers (in db but tailscale says offline)
      - Updated workers (IPs have changed)
      - Recently online workers (previously offline but now back online)
      - Offline workers (in db but not in tailscale)
    '''
    existing_workers = Worker.select()
    tailscale_workers = self.tailscale.get_by_tag(get_env("WORKER_TAILSCALE_TAG"))

    logger.info(f"Existing workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in existing_workers]}")
    logger.info(f"Tailscale workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in tailscale_workers]}")

    for worker in tailscale_workers:
      existing_worker = next((w for w in existing_workers if w.hostname == worker.hostname), None)
      if existing_worker:
        if existing_worker.online and not worker.online:
          # worker went offline
          logger.info(f"Worker {worker.hostname} went offline.")
          self.set_worker_offline(worker)
        elif not existing_worker.online and worker.online:
          # worker came back online
          logger.info(f"Worker {worker.hostname} came back online.")
          self.set_worker_online(worker)

        if existing_worker.ip != worker.ip:
          # worker IP changed
          logger.info(f"Worker {worker.hostname} IP changed from {existing_worker.ip} to {worker.ip}.")
          self.update_worker(worker)
      else:
        # new worker
        logger.info(f"New worker {worker.hostname} detected.")
        self.setup_worker(worker)

    for existing_worker in existing_workers:
      if not any(w.hostname == existing_worker.hostname for w in tailscale_workers) and existing_worker.online:
        # worker went offline or was removed
        logger.info(f"Worker {existing_worker.hostname} not found in tailscale. Presumed it went offline.")
        self.set_worker_offline(existing_worker)

  def setup_worker(self, worker):
    '''
      Setup a new worker -
      - Add to Manager database
      - Create Cloudflare DNS entry (*.int) for Tailscale routing
      - Fetch a Cloudflare Tunnel Token
      - Sync Compose, Traefik, Cloudflared files
      - Start Compose
    '''
    logger.info(f"Adding worker {worker.hostname} to manager.")
    try:
      Worker.create(hostname=worker.hostname, ip=worker.ip)
      self.cloudflare.create_dns_record(name=f"*.int.{get_env('DOMAIN')}", content=worker.ip, comment=f"{get_env('ORG')}-sage-worker-{worker.hostname}", type="A")
      tunnel = self.cloudflare.get_tunnel_token()

      # Sync files
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/docker-compose.yml", f"{worker_home_dir}/docker-compose.yml")
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/worker.env", f"{worker_home_dir}/.env", {
        "SAGE_HOME": worker_home_dir,
        "TS_IP": worker.ip,
        "TS_HOSTNAME": worker.hostname,
        "TUNNEL_TOKEN": tunnel.token
      })
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/traefik/traefik.yml", f"{worker_home_dir}/traefik/traefik.yml", {
        "ADMIN_EMAIL": get_env("ADMIN_EMAIL")
      })
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/traefik/config.yml", f"{worker_home_dir}/traefik/dynamic/config.yml", {
        "DOMAIN": get_env("DOMAIN")
      })
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/vector/vector.yml", f"{worker_home_dir}/vector/config/vector.yml", {
        "IP": self.tailscale.ip(),
        "HOSTNAME": worker.hostname
      })
      
      # Start containers
      self.tailscale.exec_command(
        worker.hostname, 
        f"docker compose -f {worker_home_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build",
        timeout=300
      )

      Worker.update(online=True).where(Worker.hostname == worker.hostname).execute()
      logger.info(f"Worker {worker.hostname} added to manager.")
    except Exception as e:
      # Cleanup on failure
      logger.error(f"Failed to setup worker {worker.hostname} : {e}")
      self.remove_worker(worker)
      raise Exception(f"Failed to setup worker {worker.hostname} : {e}")

  def remove_worker(self, worker):
    '''
      A removed worker is assumed to have been destoryed externally (VM + Tailscale) -
      - Remove from Manager database
      - Delete Cloudflare DNS entry (*.int) for Tailscale routing
      - Cloudflare automatically cleans up dead tunnels
    '''
    logger.info(f"Removing worker {worker.hostname} from manager.")
    Worker.delete().where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.delete_dns_record(name=f"*.int.{get_env('DOMAIN')}", content=worker.ip)
    logger.info(f"Worker {worker.hostname} removed from manager.")

  def set_worker_offline(self, worker):
    '''
      A worker can be set to offline if it is expected to come back soon, e.g. after a reboot -
      - Mark as offline in Manager database
      - Delete Cloudflare DNS entry (*.int) for Tailscale routing
      - Cloudflare will automatically handle an offline tunnel
    '''
    logger.info(f"Setting worker {worker.hostname} offline.")
    Worker.update(online=False).where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.delete_dns_record(name=f"*.int.{get_env('DOMAIN')}", content=worker.ip)
    logger.info(f"Worker {worker.hostname} set to offline.")

  def set_worker_online(self, worker):
    '''
      - Mark as online in Manager database
      - Create Cloudflare DNS entry (*.int) for Tailscale routing
      - Cloudflare will automatically handle tunnel re-connection
    '''
    logger.info(f"Setting worker {worker.hostname} online.")
    Worker.update(online=True).where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.create_dns_record(name=f"*.int.{get_env('DOMAIN')}", content=worker.ip, comment=f"{get_env('ORG')}-sage-worker-{worker.hostname}", type="A")
    logger.info(f"Worker {worker.hostname} set to online.")

  def update_worker(self, worker):
    '''
      Update a worker's IP address -
      - Update in Manager database
      - Update Cloudflare DNS entry (*.int) for Tailscale routing
    '''
    logger.info(f"Updating worker {worker.hostname} with ip {worker.ip}.")
    Worker.update(ip=worker.ip).where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.create_dns_record(name=f"*.int.{get_env('DOMAIN')}", content=worker.ip, comment=f"{get_env('ORG')}-sage-worker-{worker.hostname}", type="A")
    logger.info(f"Worker {worker.hostname} updated with new IP {worker.ip}.")

  def sync_application_status(self):
    '''
      Sync Application & Container status from the workers.
      Ideally status is managed explicitly so this is to catch unexpected changes 
      like a container being stopped from the worker side or a worker going offline without Manager knowing yet.
    '''
    # Get all application containers
    containers = Container.select()

    # Get the container status from each worker
    container_status = {}
    workers = Worker.select()
    for worker in workers:
      if worker.online:
        _, docker_ps_output = self.tailscale.exec_command(worker.hostname, "docker ps --format '{{.Names}}|{{.State}}'")
        for line in docker_ps_output:
          try:
            container_name, container_state = line.split("|")
          except Exception:
            continue
          container_status[f"{worker.hostname}-{container_name}"] = container_state

    for container in containers:
      # Skip state update during explicit actions
      if container.status in ['stopping', 'deploying']:
        continue

      worker_container_name = f"{container.worker.hostname}-{container.application.project.name}-{container.application.name}"
      status = container_status.get(worker_container_name)
      if status:
        if status == "running" and container.status != "active":
          container.status = "active"
          container.save()
          logger.error(f"Marking application container {worker_container_name} as active as container is running.")
        elif status in ["paused", "restarting"] and container.status != "error":
          container.status = "error"
          container.save()
          logger.error(f"Marking application container {worker_container_name} as error as container is in state {status}.")
      else:
        # if no status is found and container is supposed to be active, mark as error (could be offline or stopped container)
        if container.status == 'active':
          container.status = "error"
          container.save()
          logger.error(f"Marking application container {worker_container_name} as error as status not found (worker may be offline or container stopped).")
  
  def sync_application_traefik_domains_config(self, application: Application):
    '''
      Sync Traefik domains config for an application.
    '''
    # Determine the online load-balanced servers
    container_name = f"{application.project.name}-{application.name}"
    active_containers = [container for container in application.containers if container.worker.online and container.status == "active"]
    logger.info(f"Syncing Traefik config for application {container_name} to these workers {[container.worker.hostname for container in active_containers]}.")

    # Run per worker
    for container in application.containers:
      # Worker if offline so can't do anything anyway
      if not container.worker.online:
        logger.error(f"Worker {container.worker.hostname} is offline, skipping Traefik config sync for application {container_name} on this worker.")

      # Clear any config on this worker
      elif container.status != "active":
        self.tailscale.exec_command(container.worker.hostname, f"rm -f {worker_home_dir}/traefik/dynamic/{container_name}-*.yml")
        logger.error(f"Worker {container.worker.hostname} container is not active, skipping Traefik config sync for application {container_name} on this worker.")

    # Application is not active (maybe during deploying/stopping)
    if application.status != "active":
      logger.error(f"Worker {container.worker.hostname} container is active but application status is {application.status}, skipping Traefik config sync for application {container_name} on this worker.")
      return

    # Application is active -> sync Traefik config based on active containers
    for container in active_containers:
      self.tailscale.exec_command(container.worker.hostname, f"rm -f {worker_home_dir}/traefik/dynamic/{container_name}-*.yml")

      for domain in application.domains:
        traefik_config_template = "service_internal.yml" if domain.type == "internal" else "service_public.yml"
        # Mesh communication between Traefiks always uses port 80 (HTTP)
        load_balanced_servers = [f"{{ url: \"http://{c.worker.ip}:80\" }}" for c in active_containers]
        self.tailscale.sync_file(container.worker.hostname, 
                                  app_dir / f"templates/worker/traefik/{traefik_config_template}", 
                                  f"{worker_home_dir}/traefik/dynamic/{container_name}-{domain.type}-{domain.name}.yml", 
                                  {
                                    "SERVICE": container_name,
                                    "DOMAIN": get_env("DOMAIN"),
                                    "PORT": domain.port,
                                    "LOAD_BALANCED_SERVERS": ", ".join(load_balanced_servers)
                                  })
  
    application.domains_synced = True
    application.save()
    logger.info(f"Traefik config synced for application {container_name}.")

  async def deploy_application(self, application: Application):
    '''
      Deploy an application.
    '''
    application.status = "deploying"
    application.save()
    logger.info(f"Deploying application {application.name}...")

    await asyncio.gather(*[
      self.deploy_application_container(container) 
      for container in application.containers
    ], return_exceptions=False)
    
    if any(container.status == "error" for container in application.containers):
      application.status = "error"
      application.save()
      raise Exception(f"Failed to deploy application {application.name}.")
    else:
      application.status = "active"
      application.save()
      logger.info(f"Application {application.name} deployed.")

  def delete_container(self, container: Container):
    '''
      Delete a container.
    '''
    # Create a deployment for tracking in case of error
    Deployment.create(container=container, type='delete', application_task_id=task_id.get(), container_task_id=task_id.get())
    container.status = "stopping"
    container.save()
    logger.info(f"Deleting container of application {container.application.name} from worker {container.worker.hostname}")

    container_dir = f"{worker_home_dir}/applications/{container.application.name}"

    try:
      # Stop container on worker
      self.tailscale.exec_command(
        container.worker.hostname, 
        f"[ ! -d \"{container_dir}\" ] || docker compose -f \"{container_dir}/docker-compose.yml\" down --volumes --rmi all --remove-orphans", 
        timeout=60
      )

      # Remove application folder
      self.tailscale.exec_command(
        container.worker.hostname, 
        f"rm -rf {container_dir}", 
        timeout=30
      )

      # Delete database record
      container.delete_instance()
    except Exception as e:
      container.status = "error"
      container.save()
      logger.error(f"Failed to delete container {container.id} of application {container.application.name} from worker {container.worker.hostname}: {e}")
      raise Exception(f"Failed to delete container {container.id} of application {container.application.name} from worker {container.worker.hostname}: {e}")
    
  async def stop_application(self, application: Application):
    '''
      Stop an application.
    '''
    application.status = "stopping"
    application.save()
    logger.info(f"Stopping application {application.name}...")

    await asyncio.gather(*[
      self.stop_application_container(container) 
      for container in application.containers
    ], return_exceptions=False)
    
    if any(container.status == "error" for container in application.containers):
      application.status = "error"
      application.save()
      raise Exception(f"Failed to stop application {application.name}.")
    else:
      application.status = "inactive"
      application.save()
      logger.info(f"Application {application.name} stopped.")

  async def stop_application_container(self, container: Container):
    # Create a deployment for tracking with a different task id
    container_task_id = generate_task_id_token()
    Deployment.create(container=container, type='stop', application_task_id=task_id.get(), container_task_id=container_task_id)
    container.status = "stopping"
    container.save()
    logger.info(f"Stopping application {container.application.name} container on worker {container.worker.hostname} with task id {container_task_id}...")

    exception_message = None

    try:
      task_id_token = task_id.set(container_task_id)

      container_name = f"{container.application.project.name}-{container.application.name}"
      container_dir = f"{worker_home_dir}/applications/{container.application.name}"

      app_env = container.application.env if container.application.env else ""
      app_build_args = container.application.args if container.application.args else ""

      # Stop with docker compose
      await run_in_executor_with_context(self.tailscale.exec_command, container.worker.hostname,
                                         f"docker compose -f {container_dir}/docker-compose.yml down")

      deployment_status = "inactive"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "inactive":
      logger.info(f"Application {container.application.name} container stopped on worker {container.worker.hostname} with task id {container_task_id}.")
    else: 
      logger.error(f"Failed to stop application {container.application.name} container on worker {container.worker.hostname} with task id {container_task_id}. Exception: {exception_message}")

  async def deploy_application_container(self, container: Container):
    # Create a deployment for tracking with a different task id
    container_task_id = generate_task_id_token()
    Deployment.create(container=container, type='deploy', application_task_id=task_id.get(), container_task_id=container_task_id)
    container.status = "deploying"
    container.save()
    logger.info(f"Deploying application {container.application.name} container to worker {container.worker.hostname} with task id {container_task_id}...")

    exception_message = None

    try:
      task_id_token = task_id.set(container_task_id)

      container_name = f"{container.application.project.name}-{container.application.name}"
      container_dir = f"{worker_home_dir}/applications/{container.application.name}"

      app_env = container.application.env if container.application.env else ""
      app_build_args = container.application.args if container.application.args else ""
      
      # Create the secrets file
      await run_in_executor_with_context(self.tailscale.sync_file, container.worker.hostname, 
                                        app_dir / "templates/worker/file", f"{container_dir}/.env", {
                                          "CONTENT": app_env
                                        })
        
      # Create the build arguments file
      await run_in_executor_with_context(self.tailscale.sync_file, container.worker.hostname, 
                                      app_dir / "templates/worker/file", f"{container_dir}/build.args", {
                                        "CONTENT": app_build_args
                                      })
      
      # Create the compose file based on application type
      if container.application.type == "docker":
        await run_in_executor_with_context(self.tailscale.sync_file, container.worker.hostname, 
                                           app_dir / "templates/worker/application/dockerhub-compose.yml", f"{container_dir}/docker-compose.yml", {
                                            "APPLICATION_NAME": container.application.name,
                                            "CONTAINER_NAME": container_name,
                                            "IMAGE": container.application.image
                                          })
      elif container.application.type == "git":
        await run_in_executor_with_context(self.tailscale.sync_file, container.worker.hostname, 
                                         app_dir / "templates/worker/application/gitrepo-compose.yml", f"{container_dir}/docker-compose.yml", {
                                            "APPLICATION_NAME": container.application.name,
                                            "CONTAINER_NAME": container_name,
                                            "REPO": container.application.repo,
                                            "DOCKERFILE": container.application.path,
                                         })

      # Deploy with docker compose
      await run_in_executor_with_context(self.tailscale.exec_command, container.worker.hostname,
                                         f"docker compose -f {container_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build")

      deployment_status = "active"
    except Exception as e:
      deployment_status = "error"
      exception_message = str(e)
    finally:
      task_id.reset(task_id_token)

    container.status = deployment_status
    container.save()

    if deployment_status == "active":
      logger.info(f"Application {container.application.name} container deployed to worker {container.worker.hostname} with task id {container_task_id}.")
    else: 
      logger.error(f"Failed to deploy application {container.application.name} container to worker {container.worker.hostname} with task id {container_task_id}. Exception: {exception_message}")