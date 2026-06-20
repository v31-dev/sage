import asyncio
import logging
import json
import os
from pathlib import Path

from services.base import Base
from services.settings import Settings
from services.db import Worker
from utils.common import get_env
from utils.executor import run_in_executor_with_context

app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)


class Traefik(Base):
  def __init__(self, manager):
    super().__init__()

    self.manager = manager
    self.config_path = "/etc/traefik"

    self.load()

  def load(self, clear_certificates=False):
    self.admin_email = Settings().get("cloudflare", "admin_email")
    self.domain = Settings().get("cloudflare", "domain")

    if not self.admin_email or not self.domain:
      raise ValueError("Platform settings require non-empty domain and admin_email values.")

    if clear_certificates:
      self.clear_certificates()

    # Static Traefik config
    os.makedirs(self.config_path, exist_ok=True)

    # Cloudflare DNS API token file (Traefik reads via CLOUDFLARE_DNS_API_TOKEN_FILE
    # so token rotation only needs a container restart, not a recreate).
    token_path = Path(self.config_path) / "cloudflare_dns_api_token"
    token_path.write_text(Settings().get("cloudflare", "api_token"))
    token_path.chmod(0o600)

    with open(app_dir / "templates/manager/traefik/traefik.yml", "r") as f:
      traefik_config = f.read()
      traefik_config = traefik_config.replace("${ADMIN_EMAIL}", self.admin_email)
      with open(f"{self.config_path}/traefik.yml", "w") as f:
        f.write(traefik_config)

    # Dynamic Traefik config
    os.makedirs(f"{self.config_path}/dynamic", exist_ok=True)
    with open(app_dir / "templates/manager/traefik/config.yml", "r") as f:
      traefik_config = f.read()
      traefik_config = traefik_config.replace("${DOMAIN}", self.domain)
      with open(f"{self.config_path}/dynamic/config.yml", "w") as f:
        f.write(traefik_config)

    # Traefik config for core services
    core_services = []

    if get_env("ENV", fallback="") == "development":
      core_services.append(("ui", 5173, f"Host(`sage.core.{self.domain}`)"))
      core_services.append(("sage", 9000, f"Host(`sage.core.{self.domain}`) && PathPrefix(`/api`)"))
    else:
      core_services.append(("sage", 9000, f"Host(`sage.core.{self.domain}`)"))

    for service, port, rule in core_services:
      with open(app_dir / "templates/manager/traefik/service.yml", "r") as f:
        traefik_config = f.read()
        traefik_config = traefik_config.replace("${DOMAIN}", self.domain)
        traefik_config = traefik_config.replace("${SERVICE_NAME}", service)
        traefik_config = traefik_config.replace("${PORT}", str(port))
        traefik_config = traefik_config.replace("${RULE}", rule)
        with open(f"{self.config_path}/dynamic/{service}.yml", "w") as f:
          f.write(traefik_config)

  def clear_certificates(self):
    acme_path = Path(self.config_path) / "acme.json"
    if acme_path.exists():
      acme_path.unlink()

  def has_valid_certificates(self):
    acme_path = Path(self.config_path) / "acme.json"

    if not acme_path.exists():
      return False

    try:
      raw = acme_path.read_text().strip()
      if not raw:
        return False

      acme_data = json.loads(raw)

      # Check certificates exist
      certificates = acme_data.get("cloudflare", {}).get("Certificates")
      if not (isinstance(certificates, list) and len(certificates) > 0):
        return False

      # Check if domain is covered by the certs
      for cert in certificates:
        domain = cert.get("domain", {}).get("main")
        if self.domain == domain:
          return True

      return False
    except Exception:
      return False

  async def sync_certificates_to_workers(self):
    # Check for certs as they can take time to be provisioned
    acme_valid = False

    for _ in range(20):
      if self.has_valid_certificates():
        acme_valid = True
        break
      await asyncio.sleep(30)

    if not acme_valid:
      self.manager.notify(
          "Valid Traefik acme.json file not found after waiting. Skipping certificate sync to workers.",
          type="error"
      )

    # Workers are independent, so sync them concurrently on the app lane (the
    # async parent holds no pool worker; the rsync leaves fan out and drain).
    online_workers = list(Worker.select().where(Worker.online))
    await asyncio.gather(
        *[self.sync_certificates_to_worker(worker, acme_valid) for worker in online_workers],
        return_exceptions=False,
    )

  async def sync_certificates_to_worker(self, worker: Worker, acme_valid: bool):
    # Workers auto-reload acme.json, so no Traefik restart is needed here.
    if acme_valid:
      await run_in_executor_with_context(
          self.manager.tailscale.sync_file,
          worker.hostname,
          f"{self.config_path}/acme.json",
          "/opt/sage/traefik/acme.json",
      )
    await run_in_executor_with_context(
        self.manager.tailscale.sync_file,
        worker.hostname,
        app_dir / "templates/worker/traefik/traefik.yml",
        f"{self.manager.worker_home_dir}/traefik/traefik.yml",
        {"ADMIN_EMAIL": self.admin_email},
    )
    logger.info(f"Finished syncing Traefik certificates to worker {worker.hostname}.")
