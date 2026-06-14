import asyncio
import fnmatch
import logging
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial

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


class ExcludeLoggerFilter(logging.Filter):
  def __init__(self, logger_names: list[str]):
    super().__init__()
    self.logger_names = logger_names

  def filter(self, record: logging.LogRecord) -> bool:
    return not any(
        record.name == logger_name or record.name.startswith(f"{logger_name}.")
        for logger_name in self.logger_names
    )


uvicorn_access_logger = logging.getLogger("uvicorn.access")

_CPU_COUNT = max(1, os.cpu_count() or 1)
# Fallback pool for blocking offloads made outside a queued task (e.g. the
# /restart route and log ingestion). Queued operations instead offload to their
# declared pool, bound per-task via `active_executor`.
_FALLBACK_EXECUTOR = ThreadPoolExecutor(
    max_workers=2 if _CPU_COUNT == 1 else min(3, _CPU_COUNT),
    thread_name_prefix="sage-fallback",
)

# Set by TaskQueue.run_task to the running task's executor pool, so blocking
# offloads inside an operation flow to that pool without each call site naming it.
active_executor: ContextVar[ThreadPoolExecutor | None] = ContextVar(
    "active_executor", default=None)


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


def run_in_executor_with_context(
    func,
    *args,
    executor: ThreadPoolExecutor | None = None,
    **kwargs,
):
  """Run a sync function in a thread pool with the current context preserved.

  Pool resolution: an explicit ``executor``, else the running task's pool
  (``active_executor``, set by TaskQueue.run_task), else the fallback pool.
  """
  ctx = copy_context()
  loop = asyncio.get_running_loop()
  return loop.run_in_executor(
      executor or active_executor.get() or _FALLBACK_EXECUTOR,
      partial(ctx.run, func, *args, **kwargs),
  )


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
  handler.addFilter(ExcludeLoggerFilter(["rocketry.task"]))

  root_logger = logging.getLogger()
  root_logger.setLevel(get_log_level())
  root_logger.handlers.clear()
  root_logger.addHandler(handler)

  logging.getLogger("httpx").setLevel(logging.WARNING)
  # Exclude some paths from logs
  logging.getLogger("uvicorn.access").addFilter(ExactPathFilter(fastapi_exclude_log_paths))

  for logger_name in [
      "rocketry.scheduler",
      "uvicorn",
      "uvicorn.access",
      "uvicorn.error",
  ]:
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.propagate = True
