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
  logger.critical(f"Error during startup: {e}")
  sys.exit(1)


# Start the FastAPI server and the scheduler
class Server(uvicorn.Server):
  def handle_exit(self, sig: int, frame) -> None:
    scheduler.shutdown()
    return super().handle_exit(sig, frame)


class VectorServer(uvicorn.Server):
  def handle_exit(self, sig: int, frame) -> None:
    self.should_exit = True
    return super().handle_exit(sig, frame)


async def main():
  # Perform manager async initialization
  await manager.async_init()

  # Start the cron/interval scheduler on this running loop.
  scheduler.start()

  vector_server = VectorServer(
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

  server = Server(
      config=uvicorn.Config(
          app_fastapi,
          host="0.0.0.0",
          port=int(os.getenv("PORT", 9000)),
          workers=1,
          loop="asyncio",
          log_config=None,
      )
  )

  api = asyncio.create_task(server.serve())
  vector = asyncio.create_task(vector_server.serve())

  await asyncio.wait([api, vector])


if __name__ == "__main__":
  asyncio.run(main())
