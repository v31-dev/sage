import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.db import Application, Project, Worker
from services.manager import Manager
from services.manager.settings import SETTING_SECTIONS
from services.metrics import Metrics
from utils.common import get_env, utcnow

logger = logging.getLogger(__name__)

# APScheduler is used only for job scheduling
_scheduler = AsyncIOScheduler(
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
)


async def dispatch_tick():
  Manager().dispatch_tick()

# --- Triggers: each only enqueues onto the Manager operation queue -------------


async def observe_workers():
  Manager().request_worker_observation()


async def refresh_worker_paths():
  Manager().request_worker_paths()


async def observe_containers():
  Manager().request_container_observation()


async def list_container_records():
  Manager().request_record_listings()


async def schedule_application_backups():
  Manager().request_automatic_backups(utcnow())


async def reconcile_domains():
  Manager().request_domain_reconciliation()


async def collect_metrics():
  # The manager collects its own container metrics in-process (cgroup/statvfs);
  Manager().add_task(
      task=Metrics().collect_self,
      scopes={f"metrics:{get_env('TS_NAME')}"},
      executor="metrics",
      quiet=True,
  )

  # The workers' metrics arrive in their reports; this writes the minutes that have completed, and gives the
  # worker list each worker's newest.
  Manager().add_task(
      task=Manager().flush_worker_metrics,
      scopes={"metrics:workers"},
      executor="metrics",
      quiet=True,
  )


async def send_summary_notification():
  Manager().request_summary()


async def refresh_latest_release():
  Manager().request_release_check()


async def backup_database():
  Manager().request_platform_backup()


async def start_work():
  Manager().request_startup()


async def cleanup():
  Manager().request_cleanup()


async def renew_certificates():
  Manager().request_certificate_renewal()


async def check_settings():
  Manager().request_settings_checks()


# Job scheduling
_JOBS = [
    (dispatch_tick, IntervalTrigger(seconds=1)),
    (observe_workers, IntervalTrigger(seconds=30)),
    (refresh_worker_paths, IntervalTrigger(seconds=60)),
    (observe_containers, CronTrigger.from_crontab("* * * * *")),
    (list_container_records, CronTrigger.from_crontab("*/10 * * * *")),
    (schedule_application_backups, CronTrigger.from_crontab("* * * * *")),
    (collect_metrics, CronTrigger.from_crontab("* * * * *")),
    (refresh_latest_release, CronTrigger.from_crontab("0 */4 * * *")),
    (backup_database, CronTrigger.from_crontab("0 */12 * * *")),
    (send_summary_notification, CronTrigger.from_crontab("0 8 * * *")),
    (cleanup, CronTrigger.from_crontab("0 4 * * *")),
    (renew_certificates, CronTrigger.from_crontab("0 3 * * *")),
    (check_settings, CronTrigger.from_crontab("*/5 * * * *")),
    (reconcile_domains, CronTrigger.from_crontab("*/5 * * * *")),
]

# Run once right after startup, as queued tasks, so the servers are already up: what runs when Sage starts
# (`Manager().request_startup()`).
_AT_STARTUP = [start_work]


def start():
  """Register every trigger and start the scheduler. Call from within the
  running event loop (AsyncIOScheduler binds to the loop on start)."""
  for fn, trigger in _JOBS:
    _scheduler.add_job(fn, trigger, id=fn.__name__, name=fn.__name__, replace_existing=True)
  for fn in _AT_STARTUP:
    _scheduler.add_job(fn, id=f"{fn.__name__}_at_startup", name=f"{fn.__name__}_at_startup")
  _scheduler.start()


def shutdown():
  """Stop the scheduler without waiting for running jobs (they are short
  enqueue calls; the operation queue drains separately)."""
  if _scheduler.running:
    _scheduler.shutdown(wait=False)
