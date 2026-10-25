import logging

import config
from services.base import Base
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.db import Database
from services.logs import Logs
from services.metrics import Metrics
from services.notification import Notifications
from services.reports import Reports
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
from .settings import SettingsMixin
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
    SettingsMixin,
    QueueMixin,
    Base,
):
  worker_home_dir = config.WORKER_HOME_DIR
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

      # Last check result per settings section (services/manager/settings.py).
      self.setting_checks = {}
      # Worker counts as of the last sync (services/manager/workers.py).
      self.worker_summary = None
      # When the stack of each worker was last started for being down, until its cooldown is over
      # (services/manager/workers.py).
      self.worker_stack_repairs = {}
      # The reboots the manager asked of workers that may still be under way, by worker
      # (services/manager/workers.py).
      self.worker_reboots = {}

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
      Metrics().start_sampler()
      Reports()
      self.tailscale = Tailscale()
      self.cloudflare = Cloudflare()
      self.certs = Certs()
      self.s3 = S3()
      self.refresh_worker_summary()

      self.notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")


__all__ = ["Manager"]
