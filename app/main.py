import asyncio
import contextlib
import logging
import os
import sys

import uvicorn
from cryptography.fernet import InvalidToken

import config
from api import app_asgi, app_redirect
from api_ingest import app as app_fastapi_ingest
from scheduler import shutdown as shutdown_scheduler
from scheduler import start as start_scheduler
from services.manager import Manager
from utils.logging import setup_logger

setup_logger()

logger = logging.getLogger(__name__)


def caused_by(error: BaseException | None, kind: type) -> bool:
  # Services re-raise setup errors as generic exceptions; the original is in the chain.
  while error is not None:
    if isinstance(error, kind):
      return True
    error = error.__cause__ or error.__context__
  return False


try:
  manager = Manager()
except Exception as e:
  if caused_by(e, InvalidToken):
    # The settings, written on the first boot, don't decrypt: the key was changed since.
    logger.critical(
        "ENCRYPTION_KEY is not the key this data was written with. Set the original key, or "
        "wipe the volume to start over with this one.")
  else:
    logger.critical(f"Error during startup: {e}", exc_info=True)
  sys.exit(1)

# Cloudflare is part of the platform: Sage serves only once its domain is set up.
try:
  manager.setup_domain()
except Exception as e:
  logger.critical(
      f"Could not set up {manager.cloudflare.manager_hostname}: {e}. Check CLOUDFLARE_API_TOKEN "
      "and DOMAIN.", exc_info=True)
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
    await manager.tailscale.close()
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

  # What the workers push: Telegraf's reports and Vector's logs. Both are left out of the access
  # log (`fastapi_exclude_log_paths`): a report arrives every few seconds from every worker. Its
  # `access_log` stays on, since turning it off silences the access logger every server shares.
  ingest_server = BackgroundServer(
      config=uvicorn.Config(
          app_fastapi_ingest,
          host="0.0.0.0",
          port=int(os.getenv("INGEST_PORT", 9001)),
          workers=1,
          loop="asyncio",
          log_config=None,
          timeout_keep_alive=config.REPORT_KEEP_ALIVE_SECONDS,
      )
  )

  if os.getenv("ENV") == "development":
    # The dev Traefik terminates TLS for the manager's address and proxies here.
    servers = [Server(config=uvicorn.Config(
        app_asgi, host="0.0.0.0", port=int(os.getenv("PORT", 9000)), workers=1, loop="asyncio",
        log_config=None))]
  else:
    # The UI and API only at https://<TS_NAME>.core.<DOMAIN>; plain HTTP redirects there.
    api_config = uvicorn.Config(
        app_asgi,
        host="0.0.0.0",
        port=int(os.getenv("HTTPS_PORT", 443)),
        workers=1,
        loop="asyncio",
        log_config=None,
        ssl_certfile=str(manager.certs.manager.fullchain_path),
        ssl_keyfile=str(manager.certs.manager.key_path),
    )
    api_config.load()
    manager.certs.tls_context = api_config.ssl
    servers = [
        Server(api_config),
        BackgroundServer(config=uvicorn.Config(
            app_redirect, host="0.0.0.0", port=int(os.getenv("PORT", 80)), workers=1, loop="asyncio",
            log_config=None)),
    ]

  tasks = [asyncio.create_task(server.serve()) for server in (*servers, ingest_server)]

  await asyncio.wait(tasks)


if __name__ == "__main__":
  asyncio.run(main())
