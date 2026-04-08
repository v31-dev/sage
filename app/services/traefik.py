import logging
import os
from pathlib import Path

from services.base import Base
from services.tailscale import Tailscale
from utils.common import get_env

app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)


class Traefik(Base):
    def __init__(self):
        super().__init__()

        self.config_path = "/etc/traefik"
        self.admin_email = get_env("ADMIN_EMAIL")
        self.domain = get_env("DOMAIN")

        # Static Traefik config
        os.makedirs(self.config_path, exist_ok=True)
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

        if get_env("ENV") == "development":
            core_services.append(("ui", 5173, f"Host(`sage.core.{self.domain}`)"))
            core_services.append(
                ("sage", 9000, f"Host(`sage.core.{self.domain}`) && PathPrefix(`/api`)")
            )
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

    def sync_certificates_to_workers(self):
        workers = Tailscale().get_by_tag(get_env("WORKER_TAILSCALE_TAG"))
        for worker in workers:
            logger.info(f"Syncing Traefik certificates to worker {worker.hostname}.")
            Tailscale().sync_file(
                worker.hostname,
                f"{self.config_path}/acme.json",
                "/opt/sage/traefik/acme.json",
            )
            logger.info(f"Finished syncing Traefik certificates to worker {worker.hostname}.")
