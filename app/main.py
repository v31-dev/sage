import asyncio
import logging
import os
import sys

import uvicorn

from utils.logging import setup_logger

setup_logger()
from api import app as app_fastapi
from api_vector import app as app_fastapi_vector
from scheduler import app as app_rocketry
from services.manager import Manager

logger = logging.getLogger(__name__)

try:
    # Setup the Manager
    Manager()
except Exception as e:
    logger.critical(f"Error during startup: {e}")
    sys.exit(1)


# Start the FastAPI server and Rocketry scheduler
class Server(uvicorn.Server):
    def handle_exit(self, sig: int, frame) -> None:
        app_rocketry.session.shut_down()
        return super().handle_exit(sig, frame)


class VectorServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame) -> None:
        self.should_exit = True
        return super().handle_exit(sig, frame)


async def main():
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
    sched = asyncio.create_task(app_rocketry.serve())

    await asyncio.wait([sched, api, vector])


if __name__ == "__main__":
    asyncio.run(main())
