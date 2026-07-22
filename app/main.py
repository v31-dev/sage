import asyncio
import logging
import os
import sys

import uvicorn

import scheduler
from api import app as app_fastapi
from api_vector import app as app_fastapi_vector
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
  def handle_exit(self, sig: int, frame) -> None:
    scheduler.shutdown()
    return super().handle_exit(sig, frame)


class BackgroundServer(uvicorn.Server):
  def handle_exit(self, sig: int, frame) -> None:
    self.should_exit = True
    return super().handle_exit(sig, frame)


async def main():
  # Perform manager async initialization
  await manager.async_init()

  # Start the cron/interval scheduler on this running loop.
  scheduler.start()

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
  vector_server.install_signal_handlers = False

  # API server
  api_server = Server(
      config=uvicorn.Config(
          app_fastapi,
          host="0.0.0.0",
          port=int(os.getenv("PORT", 9000)),
          workers=1,
          loop="asyncio",
          log_config=None,
      )
  )

  tasks = [
      asyncio.create_task(api_server.serve()),
      asyncio.create_task(vector_server.serve()),
  ]

  # In prod sage terminates its own TLS on :443 (sage.core.<domain>). In dev a
  # Traefik sidecar owns :443 — reusing the same issued cert — and proxies to the
  # Vite HMR UI + this API, so skip the TLS server here (the cert is still issued
  # by Certs at startup for Traefik to serve).
  if os.getenv("ENV") != "development":
    tls_config = uvicorn.Config(
        app_fastapi,
        host="0.0.0.0",
        port=int(os.getenv("HTTPS_PORT", 443)),
        workers=1,
        loop="asyncio",
        log_config=None,
        ssl_certfile=str(manager.certs.fullchain_path),
        ssl_keyfile=str(manager.certs.key_path),
    )
    tls_config.load()
    manager.certs.tls_context = tls_config.ssl
    tls_server = BackgroundServer(tls_config)
    tls_server.install_signal_handlers = False
    tasks.append(asyncio.create_task(tls_server.serve()))

  await asyncio.wait(tasks)


if __name__ == "__main__":
  asyncio.run(main())
