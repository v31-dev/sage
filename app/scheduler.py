import asyncio
import logging
from datetime import datetime

from rocketry import Rocketry
from rocketry.conds import every, minutely
from rocketry.conditions import SchedulerStarted
from rocketry.time import TimeDelta

from services.db import Application, Worker
from services.manager import Manager
from services.metrics import METRICS_EXECUTOR, Metrics
from utils.common import get_env
from utils.logging import TaskFailed, run_in_executor_with_context

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
# rejected when something is in flight (no pile-up). The force-resync flag is
# only consumed once a sync is actually queued.
@app.task(every("30 seconds"))
async def sync_workers():
  force = Manager().force_resync_pending.is_set()
  submitted = Manager().add_task(
      task=Manager().sync_workers,
      scopes={"platform", "app"},
      params={"force": force},
      executor="platform",
  )
  if submitted and force:
    Manager().force_resync_pending.clear()


# Reconcile each application's status independently (per-app scope), so one busy
# app never blocks syncing the rest. An app with an operation in flight is
# rejected (skipped) for this cycle; container-less apps are a no-op.
@app.task(minutely)
async def sync_application_status():
  for application in Application.select():
    Manager().add_task(
        task=Manager().sync_application_status,
        scopes={f"app:{application.id}"},
        params={"application_id": application.id},
        executor="app",
    )


# Dispatch scheduled application backups onto the operation queue.
@app.task(minutely)
async def schedule_application_backups():
  now = datetime.now()
  for application, volumes in Manager().get_due_volume_backups(now):
    Manager().add_task(
        task=Manager().backup_application_s3,
        scopes={f"app:{application.id}"},
        params={"application_id": application.id, "volume_ids": [volume.id for volume in volumes]},
        executor="app",
    )


# Sync Traefik domain config per application (per-app scope).
@app.task(minutely)
async def sync_application_traefik_domains_config():
  for application in Application.select().where(Application.domains_synced == False):
    Manager().add_task(
        task=Manager().sync_application_traefik_domains_config,
        scopes={f"app:{application.id}"},
        params={"application_id": application.id},
        executor="app",
    )


# Collect metrics from all online workers & self manager. Metrics/logs are not
# routed through the operation queue yet; they keep their own executor.
@app.task(minutely)
async def collect_metrics():
  worker_targets = [(w.ip, w.hostname) for w in Worker.select().where(Worker.online)]
  targets = [("host.docker.internal", get_env("HOSTNAME"))] + worker_targets

  errors = await asyncio.gather(
      *[
          run_in_executor_with_context(Metrics().collect, ip, host, executor=METRICS_EXECUTOR)
          for ip, host in targets
      ],
      return_exceptions=True,
  )

  failed = False
  for (ip, host), result in zip(targets, errors):
    if isinstance(result, Exception):
      logger.error(f"collect_metrics target failed for {host} ({ip}): {result}")
      failed = True

  if failed:
    raise TaskFailed()


@app.task(every("1 day"))
async def send_summary_notification():
  Manager().add_task(
      task=Manager().send_summary_notification,
      scopes={"common"},
      executor="common",
  )


# Refresh the latest sage release version from GitHub.
@app.task(every("6 hours"))
async def refresh_latest_version():
  Manager().add_task(
      task=Manager().get_latest_version,
      scopes={"common"},
      executor="common",
  )


# Backup the main database.
@app.task((every("6 hours") & ~SchedulerStarted(period=TimeDelta("10 minute"))), name="backup_database")
async def backup_database():
  Manager().add_task(
      task=Manager().backup_database_s3,
      scopes={"platform", "app"},
      executor="platform",
  )


# Daily clean up check. Metrics cleanup stays on its own executor; the manager
# cleanup runs as a platform-scoped op (fenced against backup/restore, but it
# does not block application work).
@app.task((every("1 day") & ~SchedulerStarted(period=TimeDelta("10 minute"))))
async def cleanup():
  await run_in_executor_with_context(Metrics().cleanup, executor=METRICS_EXECUTOR)
  Manager().add_task(
      task=Manager().cleanup,
      scopes={"platform"},
      executor="platform",
  )


# Sync Traefik wildcard certificates to workers.
@app.task((every("10 days")), name="traefik_sync_certs")
async def traefik_sync_certs():
  Manager().add_task(
      task=Manager().sync_traefik_certificates,
      scopes={"app"},
      executor="app",
  )
