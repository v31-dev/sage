import logging

import config
from services.base import Base
from services.certs import Certs
from services.db.database import Database
from services.logs import Logs
from services.notification import Notifications
from services.s3 import S3
from services.settings import Settings
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from utils.executor import LANES
from utils.queue import TaskQueue

from .backup_catalog import BackupCatalogMixin
from .backups import BackupsMixin
from .certificates import CertificatesMixin
from .containers import ContainersMixin
from .discovery import DiscoveryMixin
from .domains import DomainsMixin
from .lifecycle import LifecycleMixin
from .platform import PlatformMixin
from .queue import SCOPES, QueueMixin
from .settings import SettingsMixin
from .summary import SummaryMixin
from .workers import WorkersMixin

# TODO(36 step 4J): Metrics joins the Manager again when its step reworks it.
# from services.metrics import Metrics

logger = logging.getLogger(__name__)


class Manager(
    ContainersMixin,
    BackupsMixin,
    LifecycleMixin,
    BackupCatalogMixin,
    DomainsMixin,
    DiscoveryMixin,
    WorkersMixin,
    CertificatesMixin,
    PlatformMixin,
    SettingsMixin,
    SummaryMixin,
    QueueMixin,
    Base,
):
  worker_home_dir = config.WORKER_HOME_DIR

  def __init__(self):
    super().__init__()

    try:
      logger.info("Running Manager setup...")

      self.version = config.VERSION

      # What each owner works out from the queue at every change of it.
      self.queue_facts = [*self.tasks_queue_facts(), *self.containers_queue_facts(), *self.domains_queue_facts(),
                          *self.workers_queue_facts(), *self.platform_queue_facts(),
                          *self.settings_queue_facts()]
      self.container_state = self.containers_state_fact()
      self.worker_state = self.workers_state_fact()
      # When each worker's setup last failed or was refused, and when its stack was last started for being down, on
      # this process's monotonic clock: how often they are tried again.
      self.setup_failures = {}
      self.stack_repairs = {}
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
      Settings()
      Notifications().install(Settings().snapshot("notifications").values)
      S3().install(Settings().snapshot("s3").values)
      Certs()
      # TODO(36 step 4J): the manager's own metrics sampler.
      # Metrics().start_sampler()
      Notifications().notify("Manager started.")
    except Exception as e:
      raise Exception(f"Manager setup failed : {e}.")


__all__ = ["Manager"]
