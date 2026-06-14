import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from services.base import Base
from services.cloudflare import Cloudflare
from services.db import Database
from services.metrics import Metrics
from services.notification import Notifications
from services.s3 import S3
from services.settings import Settings
from services.tailscale import Tailscale
from services.traefik import Traefik
from utils.queue import TaskQueue

from ._common import app_dir
from .backups import BackupsMixin
from .core import CoreMixin
from .deployments import DeploymentsMixin
from .platform import PlatformMixin
from .queue import QueueMixin
from .traefik import TraefikMixin
from .workers import WorkersMixin

logger = logging.getLogger(__name__)

_CPU_COUNT = max(1, os.cpu_count() or 1)


class Manager(
    CoreMixin,
    WorkersMixin,
    TraefikMixin,
    DeploymentsMixin,
    BackupsMixin,
    PlatformMixin,
    QueueMixin,
    Base,
):
  worker_home_dir = "/opt/sage"
  s3_backup_path_platform = "/backups/platform"
  s3_backup_path_applications = "/backups/applications"
  backup_timestamp_format = "%Y%m%d_%H%M%S"
  platform_backup_in_progress = False

  def __init__(self):
    super().__init__()

    try:
      logger.info("Running Manager setup...")

      with open(app_dir / "VERSION") as f:
        self.version = f.read().strip()
      self.latest_version = self.version

      # Cross-thread signal: when set, the next periodic sync_workers tick will
      # run with force=True and clear the flag. Used by the UI resync button
      # and the platform restore flow to defer a force-resync onto the single
      # scheduled task path (so it can't race with the periodic tick).
      self.force_resync_pending = threading.Event()

      # Single-dispatcher operation queue. platform/app/common are independent
      # scope roots; platform and common get one worker each, app gets the rest.
      self.task_queue = TaskQueue(
          scopes=frozenset(["platform", "app", "common"]),
          executors={
              "platform": ThreadPoolExecutor(
                  max_workers=1,
                  thread_name_prefix="sage-platform",
              ),
              "common": ThreadPoolExecutor(
                  max_workers=1,
                  thread_name_prefix="sage-common",
              ),
              "app": ThreadPoolExecutor(
                  max_workers=max(1, (_CPU_COUNT * 2) - 2),
                  thread_name_prefix="sage-app",
              ),
          })

      # Initialize all services
      Database()
      Settings()
      Notifications()
      Metrics()
      self.tailscale = Tailscale()
      self.traefik = Traefik(self)
      self.cloudflare = Cloudflare()
      self.s3 = S3()

      self.notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")


__all__ = ["Manager"]
