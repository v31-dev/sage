import asyncio
import logging
from datetime import datetime

from rocketry.conds import every, minutely
from rocketry.conditions import SchedulerStarted
from rocketry.time import TimeDelta

from services.db import Application, Container, Worker, Setting
from services.manager import Manager
from services.metrics import METRICS_EXECUTOR, Metrics
from services.s3 import S3
from utils.common import get_env
from utils.logging import LoggedRocketry, LoggedSession, TaskFailed, run_in_executor_with_context

logger = logging.getLogger(__name__)

# Schedule tasks
app = LoggedRocketry(execution="async")
task_dispatcher = LoggedSession(app.session)


# Sync workers for setup
@app.task(every("30 seconds"))
async def manager_sync_workers():
  await run_in_executor_with_context(Manager().sync_workers)


# Sync Application status
@app.task(every("2 minutes"))
async def manager_sync_application_status():
  await run_in_executor_with_context(Manager().sync_application_status)


# Dispatch scheduled application backups.
@app.task(minutely)
async def schedule_application_backups():
  now = datetime.now()
  for application, volumes in Manager().get_due_volume_backups(now):
    task_dispatcher["backup_application"].run(
        application=application,
        volume_ids=[volume.id for volume in volumes],
    )


# Sync Traefik config for domains
@app.task(minutely)
async def sync_application_traefik_domains_config():
  applications = Application.select().where(Application.domains_synced == False)

  errors = await asyncio.gather(
      *[run_in_executor_with_context(
        Manager().sync_application_traefik_domains_config, application) for application in applications],
      return_exceptions=True,
  )

  if any(isinstance(e, Exception) for e in errors):
    raise TaskFailed()


# Collect metrics from all online workers & self manager
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
  Manager().send_summary_notification()


# Refresh the latest sage release version from GitHub
@app.task(every("6 hours"))
async def refresh_latest_version():
  await run_in_executor_with_context(Manager().get_latest_version)


# Backup the main database
@app.task((every("6 hours") & ~SchedulerStarted(period=TimeDelta("10 minute"))), name="backup_database")
async def backup_database():
  await Manager().backup_database_s3()


# Daily clean up check
@app.task((every("1 day") & ~SchedulerStarted(period=TimeDelta("10 minute"))))
async def cleanup():
  await run_in_executor_with_context(Metrics().cleanup, executor=METRICS_EXECUTOR)
  await Manager().cleanup()


# Sync Traefik wildcard certificates to workers
@app.task((every("10 days")), name="traefik_sync_certs")
async def traefik_sync_certs():
  await Manager().sync_traefik_certificates()


# Refresh Manager + worker Traefik state after Cloudflare domain/email/token changes.
@app.task(name="refresh_traefik")
async def refresh_traefik(admin_email_changed=False, domain_changed=False, api_token_changed=False):
  await Manager().refresh_traefik(admin_email_changed=admin_email_changed,
                                  domain_changed=domain_changed,
                                  api_token_changed=api_token_changed)


# Deploy Application
@app.task(name="deploy_application", multilaunch=True)
async def deploy_application(application: Application):
  try:
    await Manager().deploy_application(application)
  except Exception as e:
    application.status = "error"
    application.save()
    logger.error(f"Failed to deploy application {application.name}: {e}")
    raise TaskFailed()


# Stop Application
@app.task(name="stop_application", multilaunch=True)
async def stop_application(application: Application):
  try:
    await Manager().stop_application(application)
  except Exception as e:
    application.status = "error"
    application.save()
    logger.error(f"Failed to stop application {application.name}: {e}")
    raise TaskFailed()


# Delete Container
@app.task(name="delete_container", multilaunch=True)
async def delete_container(container: Container, force: bool = False):
  await run_in_executor_with_context(Manager().delete_container, container, force)


# Backup Application
@app.task(name="backup_application", multilaunch=True)
async def backup_application(
    application: Application,
    volume_ids: list[int] | None = None,
):
  try:
    await Manager().backup_application_s3(application, volume_ids=volume_ids)
  except Exception:
    raise TaskFailed()


# Delete Backup from S3
@app.task(name="delete_backup_s3", multilaunch=True)
async def delete_backup_s3(s3_path):
  try:
    await S3().delete_key(s3_path, raw_path=True)
    logger.info(f"Successfully deleted S3 backup: {s3_path}")
  except Exception as e:
    logger.error(f"Failed to delete S3 backup {s3_path}: {e}")
    raise TaskFailed()
