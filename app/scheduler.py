import asyncio
import logging
from rocketry.conds import minutely, every

from utils.common import get_env
from utils.logging import LoggedRocketry, TaskFailed, run_in_executor_with_context
from services.traefik import Traefik
from services.manager import Manager
from services.metrics import Metrics
from services.db import Worker, Application, Container


logger = logging.getLogger(__name__)

# Schedule tasks
app = LoggedRocketry(execution="async")

# Sync workers for setup
@app.task(minutely)
async def manager_sync_workers(): 
  await run_in_executor_with_context(Manager().sync_workers)

# Sync Traefik wildcard certificates to workers
@app.task(every("20 days"))
async def traefik_sync_certs():
  await run_in_executor_with_context(Traefik().sync_certificates_to_workers)

# Collect metrics from all online workers & self manager
@app.task(minutely)
async def collect_metrics():
  worker_targets = await run_in_executor_with_context(
    lambda: [(w.ip, w.hostname) for w in Worker.select().where(Worker.online == True)]
  )
  targets = [("172.17.0.1", get_env("HOSTNAME"))] + worker_targets

  errors = await asyncio.gather(*[
    run_in_executor_with_context(Metrics().collect, ip, host) for ip, host in targets
  ], return_exceptions=True)

  if any(isinstance(e, Exception) for e in errors):
    raise TaskFailed()

# Clean up metrics & logs older than X days
@app.task(every("1 day"))
async def metrics_cleanup():
  await run_in_executor_with_context(Metrics().cleanup)

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
  
# Delete Container
@app.task(name="delete_container", multilaunch=True)
async def delete_container(container: Container):
  await run_in_executor_with_context(Manager().delete_container, container)