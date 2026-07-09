import fnmatch
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

LOG_FORMAT = "[%(asctime)s] [%(levelname)-8s] [%(name)-19s] [%(task_id)s] %(message)s"
task_id = ContextVar("task_id", default="")

fastapi_exclude_log_paths = [
    "/api",  # Healthcheck
    "/api/vector/logs",  # Logs ingeestion
    "/api/workers/*/logs/*",  # Logs query
    "/api/workers/*/metrics",  # Metrics query
]


def generate_task_id_token():
  return str(uuid.uuid4())[:8]


class ContextVarFilter(logging.Filter):
  def filter(self, record):
    record.task_id = task_id.get() or ""
    return True


uvicorn_access_logger = logging.getLogger("uvicorn.access")


class fastapi_middleware(BaseHTTPMiddleware):
  async def dispatch(self, request, call_next):
    token = generate_task_id_token()

    task_id.set(token)
    request.state.task_id = token

    client = f"{request.client.host}:{request.client.port}" if request.client else "-"
    full_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    response = await call_next(request)
    status_code = response.status_code
    uvicorn_access_logger.info(
        f'{client} - "{request.method} {full_path} HTTP/{request.scope["http_version"]}" {status_code}'
    )
    response.headers["X-Task-ID"] = token
    return response


class TaskFailed(Exception):
  """Raise to mark a task as failed without printing a traceback."""
  pass


class _SuppressTracebackFilter(logging.Filter):
  """Strip exception tracebacks from TaskFailed log records."""

  def filter(self, record):
    if record.exc_info and record.exc_info[0] is TaskFailed:
      record.exc_info = None
      record.exc_text = None
    return True


class ExactPathFilter(logging.Filter):
  def __init__(self, paths: list[str]):
    super().__init__()
    self.paths = paths

  def filter(self, record: logging.LogRecord) -> bool:
    try:
      message = record.getMessage()
      match = re.search(r'"[A-Z]+ ([^\s]+) HTTP', message)
      if match:
        raw_path = match.group(1)
        base_path = raw_path.split("?")[0]
        # Check each pattern against the path
        for pattern in self.paths:
          if fnmatch.fnmatch(base_path, pattern):
            return False
    except (AttributeError, IndexError):
      # Fallback for logs that don't match the expected uvicorn format
      pass
    return True


def get_log_level() -> int:
  level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
  level = logging.getLevelNamesMapping().get(level_name)
  if level is None:
    level = logging.INFO
  return level


def setup_logger():
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(logging.Formatter(LOG_FORMAT))
  # Filter on the handler runs once for every record it processes,
  # covering propagated child-logger records that bypass parent logger filters.
  handler.addFilter(ContextVarFilter())
  handler.addFilter(_SuppressTracebackFilter())

  root_logger = logging.getLogger()
  root_logger.setLevel(get_log_level())
  root_logger.handlers.clear()
  root_logger.addHandler(handler)

  logging.getLogger("httpx").setLevel(logging.WARNING)
  logging.getLogger("asyncssh").setLevel(logging.WARNING)
  # APScheduler logs every job run at INFO; keep only warnings/errors (missed
  # runs, job exceptions) so the 1s dispatch tick doesn't flood the logs.
  logging.getLogger("apscheduler").setLevel(logging.WARNING)
  # Exclude some paths from logs
  logging.getLogger("uvicorn.access").addFilter(ExactPathFilter(fastapi_exclude_log_paths))

  for logger_name in [
      "apscheduler",
      "uvicorn",
      "uvicorn.access",
      "uvicorn.error",
  ]:
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.propagate = True
