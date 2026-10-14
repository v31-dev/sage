import asyncio
import contextlib
import logging
import os
import sys

import uvicorn

from api import app as app_fastapi
from api_vector import app as app_fastapi_vector
from scheduler import shutdown as shutdown_scheduler
from scheduler import start as start_scheduler
from services.manager import Manager
from utils.logging import setup_logger

setup_logger()

logger = logging.getLogger(__name__)

# Setup the Manager
try:
  manager = Manager()
except Exception as e:
  logger.critical(f"Error during startup: {e}", exc_info=True)
  sys.exit(1)


# Start the FastAPI server and the scheduler
class Server(uvicorn.Server):
  draining = False

  def handle_exit(self, sig: int, frame) -> None:
    # A second signal stops right away.
    if self.draining:
      return super().handle_exit(sig, frame)

    # The first one drains: no new triggers or user changes, pending tasks (and any
    # queued later) are recorded as cancelled, and running tasks finish before the
    # servers stop. How long that may take is the runtime's stop grace period.
    self.draining = True
    shutdown_scheduler()
    loop = asyncio.get_running_loop()
    loop.call_soon_threadsafe(loop.create_task, self.drain(sig))

  async def drain(self, sig: int):
    manager.stop_tasks()
    running = manager.task_snapshot()["running"]
    if running:
      logger.info(f"Stopping: waiting for {len(running)} running task(s) to finish.")
    while manager.task_snapshot()["running"]:
      await asyncio.sleep(0.5)
    logger.info("Stopping: no tasks running.")
    super().handle_exit(sig, None)


class BackgroundServer(uvicorn.Server):
  # Only the API server handles stop signals, so worker logs keep arriving while it
  # drains; this one stops when the process exits.
  @contextlib.contextmanager
  def capture_signals(self):
    yield


async def main():
  # Start the cron/interval scheduler on this running loop. Startup work that touches
  # the network (release check, certificate renewal, backup discovery) is queued from
  # here, so it runs while the servers below are already serving.
  start_scheduler()

  # Vector server for worker log ingestion
  vector_server = BackgroundServer(
      config=uvicorn.Config(
          app_fastapi_vector,
          host="0.0.0.0",
          port=int(os.getenv("METRICS_PORT", 9001)),
          workers=1,
          loop="asyncio",
          log_config=None,
      )
  )

  # API server. Plain HTTP: it is reached over the tailnet, which WireGuard already
  # encrypts.
  # TODO(rework-03): serve https://<TS_NAME>.<domain> once a domain is configured, then
  # redirect this listener to it.
  api_server = Server(
      config=uvicorn.Config(
          app_fastapi,
          host="0.0.0.0",
          port=int(os.getenv("PORT", 80)),
          workers=1,
          loop="asyncio",
          log_config=None,
      )
  )

  tasks = [
      asyncio.create_task(api_server.serve()),
      asyncio.create_task(vector_server.serve()),
  ]

  await asyncio.wait(tasks)


if __name__ == "__main__":
  asyncio.run(main())
