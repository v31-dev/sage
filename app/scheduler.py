import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.db import Application, Project, Worker
from services.manager import Manager
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
      scopes={"platform", "app"},
      executor="platform",
      quiet=True,
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
  targets = [("host.docker.internal", get_env("HOSTNAME"))] + [
      (worker.ip, worker.hostname) for worker in Worker.select().where(Worker.online)
  ]
  for ip, host in targets:
    Manager().add_task(
        task=Metrics().collect,
        scopes={f"metrics:{host}"},
        params={"ip": ip, "hostname": host},
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


async def refresh_latest_version():
  Manager().add_task(
      task=Manager().get_latest_version,
      scopes={"common"},
      executor="common",
      on_conflict=OnConflict.REPLACE,
  )


async def backup_database():
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
      task=Manager().cleanup,
      scopes={"platform"},
      executor="platform",
      on_conflict=OnConflict.REPLACE,
  )


async def traefik_sync_certs():
  Manager().add_task(
      task=Manager().sync_traefik_certificates,
      scopes={"app"},
      executor="app",
      on_conflict=OnConflict.REPLACE,
  )


# Job scheduling
_JOBS = [
    (dispatch_tick, IntervalTrigger(seconds=1)),
    (sync_workers, IntervalTrigger(seconds=30)),
    (sync_application_status, CronTrigger.from_crontab("* * * * *")),
    (schedule_application_backups, CronTrigger.from_crontab("* * * * *")),
    (reconcile_traefik_configs, CronTrigger.from_crontab("* * * * *")),
    (collect_metrics, CronTrigger.from_crontab("* * * * *")),
    (refresh_latest_version, CronTrigger.from_crontab("0 */4 * * *")),
    (backup_database, CronTrigger.from_crontab("0 */6 * * *")),
    (send_summary_notification, CronTrigger.from_crontab("0 8 * * *")),
    (cleanup, CronTrigger.from_crontab("0 4 * * *")),
    (traefik_sync_certs, CronTrigger.from_crontab("0 3 * * 0")),
]


def start():
  """Register every trigger and start the scheduler. Call from within the
  running event loop (AsyncIOScheduler binds to the loop on start)."""
  for fn, trigger in _JOBS:
    _scheduler.add_job(fn, trigger, id=fn.__name__, name=fn.__name__, replace_existing=True)
  _scheduler.start()


def shutdown():
  """Stop the scheduler without waiting for running jobs (they are short
  enqueue calls; the operation queue drains separately)."""
  if _scheduler.running:
    _scheduler.shutdown(wait=False)
