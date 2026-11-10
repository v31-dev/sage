import logging

import config
from services.base import Base
from services.db.database import Database
from services.logs import Logs
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from utils.executor import LANES
from utils.queue import TaskQueue

from .containers import ContainersMixin
from .queue import SCOPES, QueueMixin
from .workers import WorkersMixin

# TODO(36 step 4X): each mixin joins the Manager again when its part reworks it.
# from services.certs import Certs
# from services.cloudflare import Cloudflare
# from services.metrics import Metrics
# from services.notification import Notifications
# from services.s3 import S3
# from services.settings import Settings
# from .application import ApplicationMixin
# from .backups import BackupsMixin
# from .core import CoreMixin
# from .domains import DomainsMixin
# from .platform import PlatformMixin
# from .settings import SettingsMixin
# from .traefik import TraefikMixin

logger = logging.getLogger(__name__)


class Manager(
    ContainersMixin,
    WorkersMixin,
    QueueMixin,
    Base,
):
  worker_home_dir = config.WORKER_HOME_DIR

  def __init__(self):
    super().__init__()

    try:
      logger.info("Running Manager setup...")

      self.version = config.VERSION
      self.latest_version = self.version
      self.latest_release = None

      # What each owner works out from the queue at every change of it.
      # TODO(36 step 4X): the settings, platform and domains queue facts.
      self.queue_facts = [*self.tasks_queue_facts(), *self.containers_queue_facts(), *self.workers_queue_facts()]
      self.container_state = self.containers_state_fact()
      self.worker_state = self.workers_state_fact()
      # Each container recovering from an interrupted operation: the incident and when it was first seen, on this
      # process's monotonic clock, so a restart waits the old work's whole allowance again.
      self.recoveries = {}
      # Each orphan waiting out an operation its record shows unsettled, by (worker, container id): the incident, when
      # it was first seen on the monotonic clock and its allowance.
      self.orphans = {}

      self.task_queue = TaskQueue(scopes=SCOPES, executors=LANES, record=self._persist_task,
                                  notify=self._queue_changed)

      # Initialize all services
      Database()
      Logs().start_capture()
      WorkerReports()
      self.tailscale = Tailscale()
      # TODO(36 step 4X): the services of the parts not reworked yet.
      # Settings()
      # Notifications().install(Settings().snapshot("notifications").values)
      # Metrics().start_sampler()
      # self.cloudflare = Cloudflare()
      # self.certs = Certs()
      # self.s3 = S3()
      # self.s3.install(Settings().snapshot("s3").values)
      # self.load_workers()
      # self.notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")


__all__ = ["Manager"]
