import logging

from services.base import Base
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.db import Database
from services.logs import Logs
from services.metrics import Metrics
from services.notification import Notifications
from services.s3 import S3
from services.settings import Settings
from services.tailscale import Tailscale
from utils.executor import APP_EXECUTOR, COMMON_EXECUTOR, METRICS_EXECUTOR, PLATFORM_EXECUTOR
from utils.queue import TaskQueue

from ._common import app_dir
from .application import ApplicationMixin
from .backups import BackupsMixin
from .core import CoreMixin
from .platform import PlatformMixin
from .queue import QueueMixin
from .traefik import TraefikMixin
from .workers import WorkersMixin

logger = logging.getLogger(__name__)


class Manager(
    CoreMixin,
    WorkersMixin,
    TraefikMixin,
    ApplicationMixin,
    BackupsMixin,
    PlatformMixin,
    QueueMixin,
    Base,
):
  worker_home_dir = "/opt/sage"
  s3_backup_path_platform = "/backups/platform"
  s3_backup_path_applications = "/backups/applications"

  def __init__(self):
    super().__init__()

    try:
      logger.info("Running Manager setup...")

      with open(app_dir / "VERSION") as f:
        self.version = f.read().strip()
      self.latest_version = self.version
      self.latest_release = None

      # Single-dispatcher operation queue. Each scope root maps to an independent
      # lane pool (all defined in utils.executor).
      self.task_queue = TaskQueue(
          scopes=frozenset(["platform", "app", "common", "metrics"]),
          executors={
              "platform": PLATFORM_EXECUTOR,
              "common": COMMON_EXECUTOR,
              "app": APP_EXECUTOR,
              "metrics": METRICS_EXECUTOR,
          },
          record=self._persist_task,
      )

      # Initialize all services
      Logs()
      Database()
      Settings()
      Notifications()
      Metrics()
      self.tailscale = Tailscale()
      self.cloudflare = Cloudflare()
      self.certs = Certs()
      self.s3 = S3()

      self.notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")

  async def async_init(self):
    """
    Perform async initialization after Manager.__init__().
    - Discovers and reconciles S3 backups with the database.
    - Fetches the latest release details.
    """
    await self.discover_s3_platform_backups()
    self.refresh_latest_release()


__all__ = ["Manager"]
