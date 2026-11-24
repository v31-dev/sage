import asyncio
import contextlib
import logging
import os
import sys

import uvicorn
from cryptography.fernet import InvalidToken

import config
from api import SOCKET_IO_PATH
from api import app as app_fastapi
from api import app_asgi, app_redirect
from api_health import app as app_fastapi_health
from api_ingest import app as app_fastapi_ingest
from scheduler import shutdown as shutdown_scheduler
from scheduler import start as start_scheduler
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.db.database import Database
from services.db.encryption import initialize_encryption
from services.db.migrator import SchemaRefused
from services.manager import Manager
from services.tailscale import Tailscale
from utils.executor import LANES, run_in_executor_with_context
from utils.logging import setup_logger, unlogged_paths

# The access log leaves out what is asked every few seconds or more: the routes marked so, and Socket.IO's polling.
setup_logger(unlogged_paths([app_fastapi, app_fastapi_ingest, app_fastapi_health], [SOCKET_IO_PATH]))

logger = logging.getLogger(__name__)


def caused_by(error: BaseException | None, kind: type) -> BaseException | None:
  # Services re-raise setup errors as generic exceptions; the original is in the chain.
  while error is not None:
    if isinstance(error, kind):
      return error
    error = error.__cause__ or error.__context__
  return None


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
    Manager().stop_tasks()
    running = Manager().task_snapshot()["running"]
    if running:
      logger.info(f"Stopping: waiting for {len(running)} running task(s) to finish.")
    while Manager().task_snapshot()["running"]:
      await asyncio.sleep(0.5)
    logger.info("Stopping: no tasks running.")
    await Tailscale().close()
    Database().checkpoint()
    super().handle_exit(sig, None)


class BackgroundServer(uvicorn.Server):
  # Only the API server handles stop signals, so worker logs keep arriving while it
  # drains; this one stops when the process exits.
  @contextlib.contextmanager
  def capture_signals(self):
    yield


async def main():
  # A platform's health probe is answered from the start, while the setup below can take minutes on a first start.
  health_server = BackgroundServer(config=uvicorn.Config(
      app_fastapi_health, host="0.0.0.0", port=config.HEALTHCHECK_PORT, workers=1, loop="asyncio", log_config=None))
  health = asyncio.create_task(health_server.serve())
  while not health_server.started:
    if health.done():
      health.result()
    await asyncio.sleep(0.05)

  try:
    initialize_encryption(os.getenv("ENCRYPTION_KEY"))
  except ValueError as e:
    logger.critical(str(e))
    sys.exit(1)

  try:
    Manager()
  except Exception as e:
    if caused_by(e, InvalidToken):
      # The settings, written on the first boot, don't decrypt: the key was changed since.
      logger.critical(
          "ENCRYPTION_KEY is not the key this data was written with. Set the original key, or "
          "wipe the volume to start over with this one.")
    elif refused := caused_by(e, SchemaRefused):
      logger.critical(str(refused))
    else:
      logger.critical(f"Error during startup: {e}", exc_info=True)
    sys.exit(1)

  # Cloudflare is part of the platform: Sage serves only once its domain is set up. Nothing else uses the platform
  # lane before the scheduler starts.
  try:
    await run_in_executor_with_context(Manager().setup_domain, executor=LANES["main"])
  except Exception as e:
    logger.critical(
        f"Could not set up {Cloudflare().manager_hostname}: {e}. Check CLOUDFLARE_API_TOKEN "
        "and DOMAIN.", exc_info=True)
    sys.exit(1)

  # Start the cron/interval scheduler on this running loop. Startup work that touches
  # the network (release check, certificate renewal, backup discovery) is queued from
  # here, so it runs while the servers below are already serving.
  start_scheduler()

  # What the workers push: Telegraf's reports and Vector's logs. Both are left out of the access
  # log (`no_access_log`): a report arrives every few seconds from every worker. Its
  # `access_log` stays on, since turning it off silences the access logger every server shares.
  ingest_server = BackgroundServer(
      config=uvicorn.Config(
          app_fastapi_ingest,
          host=config.LISTEN_HOST,
          port=config.INGEST_PORT,
          workers=1,
          loop="asyncio",
          log_config=None,
          timeout_keep_alive=config.REPORT_KEEP_ALIVE_SECONDS,
      )
  )

  if os.getenv("ENV") == "development":
    # The dev Traefik terminates TLS for the manager's address and proxies here.
    servers = [Server(config=uvicorn.Config(
        app_asgi, host=config.LISTEN_HOST, port=config.DEV_API_PORT, workers=1, loop="asyncio",
        log_config=None))]
  else:
    # The UI and API only at https://<TS_NAME>.core.<DOMAIN>; plain HTTP redirects there.
    api_config = uvicorn.Config(
        app_asgi,
        host=config.LISTEN_HOST,
        port=config.HTTPS_PORT,
        workers=1,
        loop="asyncio",
        log_config=None,
        ssl_certfile=str(Certs().manager.fullchain_path),
        ssl_keyfile=str(Certs().manager.key_path),
    )
    api_config.load()
    Certs().tls_context = api_config.ssl
    servers = [
        Server(api_config),
        BackgroundServer(config=uvicorn.Config(
            app_redirect, host=config.LISTEN_HOST, port=config.HTTP_PORT, workers=1, loop="asyncio",
            log_config=None)),
    ]

  tasks = [asyncio.create_task(server.serve()) for server in (*servers, ingest_server)]

  await asyncio.wait([*tasks, health])


if __name__ == "__main__":
  asyncio.run(main())
