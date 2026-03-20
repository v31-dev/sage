import logging
from pathlib import Path

from utils.common import get_env
from services.base import Base
from services.tailscale import Tailscale
from services.cloudflare import Cloudflare
from services.traefik import Traefik
from services.db import Database, Worker
from services.metrics import Metrics


app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)

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
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/docker-compose.yml", "/opt/sage/docker-compose.yml")
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/worker.env", "/opt/sage/.env", {
        "SAGE_HOME": "/opt/sage",
        "TS_IP": worker.ip,
        "TS_HOSTNAME": worker.hostname,
        "TUNNEL_TOKEN": tunnel.token
      })
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/traefik/traefik.yml", "/opt/sage/traefik/traefik.yml", {
        "ADMIN_EMAIL": get_env("ADMIN_EMAIL")
      })
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/traefik/config.yml", "/opt/sage/traefik/dynamic/config.yml", {
        "DOMAIN": get_env("DOMAIN")
      })
      self.tailscale.sync_file(worker.hostname, app_dir / "templates/worker/vector/vector.yml", "/opt/sage/vector/config/vector.yml", {
        "IP": self.tailscale.ip(),
        "HOSTNAME": worker.hostname
      })
      
      # Start containers
      self.tailscale.exec_command(
        worker.hostname, 
        "docker compose -f /opt/sage/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build",
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