import logging
from datetime import datetime

from rocketry import Rocketry
from rocketry.conds import every, minutely
from rocketry.conditions import SchedulerStarted
from rocketry.time import TimeDelta

from services.db import Application, Project, Worker
from services.manager import Manager
from services.metrics import Metrics
from utils.common import get_env
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

# Rocketry is a pure scheduler here: it fires the functions below on a cadence;
# all execution + concurrency control lives in the Manager operation queue.
app = Rocketry(execution="async")


# Single consumer for the operation queue: starts every pending task whose
# scopes are free. Runs on the loop, not as a queued task itself.
@app.task(every("1 second"))
async def dispatch_tick():
  Manager().dispatch_tick()


# Detect worker changes. platform+app scoped so it serializes with deploys;
# rejected when something is in flight (no pile-up).
@app.task(every("30 seconds"))
async def sync_workers():
  Manager().add_task(
      task=Manager().sync_workers,
      scopes={"platform", "app"},
      executor="platform",
      quiet=True,
  )


# Reconcile each application's status independently (per-app scope), so one busy
# app never blocks syncing the rest. An app with an operation in flight is
# rejected (skipped) for this cycle; container-less apps are a no-op.
@app.task(minutely)
async def sync_application_status():
  for application in Application.select(Application, Project).join(Project):
    Manager().add_task(
        task=Manager().sync_application_status,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id},
        executor="app",
        quiet=True,
    )


# Dispatch scheduled application backups onto the operation queue.
@app.task(minutely)
async def schedule_application_backups():
  now = datetime.now()
  for application, volumes in Manager().get_due_volume_backups(now):
    Manager().add_task(
        task=Manager().backup_application_s3,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id, "volume_ids": [volume.id for volume in volumes]},
        executor="app",
    )


# Sync Traefik domain config per application (per-app scope).
@app.task(minutely)
async def sync_application_traefik_domains_config():
  for application in Application.select(Application, Project).join(Project).where(Application.domains_synced == False):
    Manager().add_task(
        task=Manager().sync_application_traefik_domains_config,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id},
        executor="app",
        quiet=True,
    )


# Collect host metrics from this manager and each online worker. One task per
# target (sibling `metrics:<host>` scopes) so a slow or flaky worker only skips
# its own cycle. Recorded only on failure, since it runs every minute.
@app.task(minutely)
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


# Long-period crons use REPLACE so a transient scope conflict doesn't skip the
# whole cycle (next run is hours/days away); latest-wins keeps no backlog.
@app.task(every("1 day"))
async def send_summary_notification():
  Manager().add_task(
      task=Manager().send_summary_notification,
      scopes={"common"},
      executor="common",
      on_conflict=OnConflict.REPLACE,
  )


# Refresh the latest sage release version from GitHub.
@app.task(every("6 hours"))
async def refresh_latest_version():
  Manager().add_task(
      task=Manager().get_latest_version,
      scopes={"common"},
      executor="common",
      on_conflict=OnConflict.REPLACE,
  )


# Backup the main database. REPLACE: supersede a pending backup (latest wins) and
# wait behind a running one, so at most one backup is queued behind the in-flight work.
@app.task((every("6 hours") & ~SchedulerStarted(period=TimeDelta("10 minute"))), name="backup_database")
async def backup_database():
  Manager().add_task(
      task=Manager().backup_database_s3,
      scopes={"platform", "app"},
      executor="platform",
      on_conflict=OnConflict.REPLACE,
  )


# Daily clean up check. Both use REPLACE so a transient conflict (a running metrics
# collect, or a backup/restore on the platform lane) defers rather than skips the
# day's cleanup; the manager cleanup is platform-scoped and does not block app work.
@app.task((every("1 day") & ~SchedulerStarted(period=TimeDelta("10 minute"))))
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


# Sync Traefik wildcard certificates to workers. REPLACE so a transient app-scope
# conflict doesn't skip the sync for another 10 days.
@app.task((every("10 days")), name="traefik_sync_certs")
async def traefik_sync_certs():
  Manager().add_task(
      task=Manager().sync_traefik_certificates,
      scopes={"app"},
      executor="app",
      on_conflict=OnConflict.REPLACE,
  )
