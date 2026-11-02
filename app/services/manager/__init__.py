import logging
import threading

import config
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
from services.worker_reports import WorkerReports
from utils.executor import LANES
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
      # The workers: what their owners pushed (`WorkerInputs` by hostname), the entry derived from each (status, path,
      # container count, and whether it answered the last sync and was set up, `online`, which is not known until a
      # sync has looked at it) (services/manager/workers.py).
      self._worker_inputs = {}
      self.worker_list = []
      self._worker_list_lock = threading.Lock()
      # What each owner works out from the queue at every change of it: the tasks and the state, the settings sections
      # applying, the restore and the backups, the worker tasks.
      self.queue_facts = [*self.tasks_queue_facts(), *self.settings_queue_facts(), *self.platform_queue_facts(),
                          *self.workers_queue_facts()]
      # When the stack of each worker was last started for being down, until its cooldown is over
      # (services/manager/workers.py).
      self.worker_stack_repairs = {}

      # Single-dispatcher operation queue. Each scope root maps to an independent
      # lane pool (all defined in utils.executor).
      self.task_queue = TaskQueue(scopes=frozenset(LANES), executors=LANES, record=self._persist_task,
                                  notify=self._queue_changed)

      # Initialize all services
      Database()
      Settings()
      Logs().start_capture()
      Notifications()
      Metrics().start_sampler()
      WorkerReports()
      self.tailscale = Tailscale()
      self.cloudflare = Cloudflare()
      self.certs = Certs()
      self.s3 = S3()
      self.load_workers()

      self.notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")


__all__ = ["Manager"]
