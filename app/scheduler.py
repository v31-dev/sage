import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.db import Application, Project, Worker
from services.logs import Logs
from services.manager import Manager
from services.manager.checks import SETTING_SECTIONS
from services.metrics import Metrics
from utils.common import get_env
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

# APScheduler is used only for job scheduling
_scheduler = AsyncIOScheduler(
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30}
)


async def dispatch_tick():
  Manager().dispatch_tick()

# --- Triggers: each only enqueues onto the Manager operation queue -------------


async def sync_workers():
  Manager().add_task(
      task=Manager().sync_workers,
      scopes={"platform"},
      executor="platform",
      quiet=True,
  )


async def check_worker_stamps():
  # The env (domain, Cloudflare account) may have changed while Sage was stopped, so
  # workers that stayed online are checked against their stamps too.
  Manager().add_task(
      task=Manager().sync_workers,
      scopes={"platform"},
      params={"check_stamps": True},
      executor="platform",
      on_conflict=OnConflict.REPLACE,
  )


async def sync_application_status():
  for application in Application.select(Application, Project).join(Project):
    Manager().add_task(
        task=Manager().sync_application_status,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id},
        executor="app",
        quiet=True,
    )


async def schedule_application_backups():
  if not Manager().s3.s3_config:
    return
  now = datetime.now()
  for application, volumes in Manager().get_due_volume_backups(now):
    Manager().add_task(
        task=Manager().backup_application_s3,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id, "volume_ids": [volume.id for volume in volumes]},
        executor="app",
    )


async def reconcile_traefik_configs():
  Manager().add_task(
      task=Manager().reconcile_traefik_configs,
      scopes={"platform"},
      executor="platform",
      quiet=True,
  )


async def collect_metrics():
  # The manager collects its own container metrics in-process (cgroup/statvfs);
  Manager().add_task(
      task=Metrics().collect_self,
      scopes={f"metrics:{get_env('TS_NAME')}"},
      executor="metrics",
      quiet=True,
  )

  # Worker metrics are polled over their Glances endpoint.
  for worker in Worker.select().where(Worker.online):
    Manager().add_task(
        task=Metrics().collect,
        scopes={f"metrics:{worker.hostname}"},
        params={"ip": worker.ip, "hostname": worker.hostname},
        executor="metrics",
        quiet=True,
    )


async def send_summary_notification():
  Manager().add_task(
      task=Manager().send_summary_notification,
      scopes={"common"},
      executor="common",
      on_conflict=OnConflict.REPLACE,
  )


async def refresh_latest_release():
  Manager().add_task(
      task=Manager().refresh_latest_release,
      scopes={"common"},
      executor="common",
      on_conflict=OnConflict.REPLACE,
  )


async def backup_database():
  if not Manager().s3.s3_config:
    return
  Manager().add_task(
      task=Manager().backup_database_s3,
      scopes={"platform", "app"},
      executor="platform",
      on_conflict=OnConflict.REPLACE,
  )


async def cleanup():
  Manager().add_task(
      task=Metrics().cleanup,
      name="metrics_cleanup",
      scopes={"metrics"},
      executor="metrics",
      quiet=True,
      on_conflict=OnConflict.REPLACE,
  )
  Manager().add_task(
      task=Logs().cleanup,
      name="logs_cleanup",
      scopes={"common"},
      executor="common",
      quiet=True,
      on_conflict=OnConflict.REPLACE,
  )
  Manager().add_task(
      task=Manager().cleanup,
      scopes={"platform"},
      executor="platform",
      on_conflict=OnConflict.REPLACE,
  )


async def renew_certificates():
  Manager().add_task(
      task=Manager().renew_certificates,
      scopes={"platform", "app"},
      executor="platform",
      on_conflict=OnConflict.REPLACE,
  )


async def discover_backups():
  if not Manager().s3.s3_config:
    return
  Manager().add_task(
      task=Manager().discover_s3_backups,
      scopes={"platform", "app"},
      executor="platform",
  )


async def check_settings():
  for key in SETTING_SECTIONS:
    if Manager().section_configured(key):
      Manager().add_task(
          task=Manager().check_setting,
          name=f"check_{key}",
          params={"key": key},
          scopes={f"common:{key}"},
          executor="common",
          on_conflict=OnConflict.REPLACE,
          quiet=True,
      )


# Job scheduling
_JOBS = [
    (dispatch_tick, IntervalTrigger(seconds=1)),
    (sync_workers, IntervalTrigger(seconds=30)),
    (sync_application_status, CronTrigger.from_crontab("* * * * *")),
    (schedule_application_backups, CronTrigger.from_crontab("* * * * *")),
    (reconcile_traefik_configs, CronTrigger.from_crontab("* * * * *")),
    (collect_metrics, CronTrigger.from_crontab("* * * * *")),
    (refresh_latest_release, CronTrigger.from_crontab("0 */4 * * *")),
    (backup_database, CronTrigger.from_crontab("0 */12 * * *")),
    (send_summary_notification, CronTrigger.from_crontab("0 8 * * *")),
    (cleanup, CronTrigger.from_crontab("0 4 * * *")),
    (renew_certificates, CronTrigger.from_crontab("0 3 * * *")),
    (check_settings, CronTrigger.from_crontab("*/5 * * * *")),
]

# Run once right after startup (as queued tasks, so the servers are already up), then on
# their cron schedule where they have one.
_AT_STARTUP = [refresh_latest_release, renew_certificates, discover_backups, check_settings, check_worker_stamps]


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
