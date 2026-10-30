import fnmatch
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable
from contextvars import ContextVar

LOG_FORMAT = "[%(asctime)s] [%(levelname)-8s] [%(name)-19s] [%(task_id)s] %(message)s"
task_id = ContextVar("task_id", default="")
# One container's part of a task (its deploy, stop or backup), logged inside the task.
sub_task_id = ContextVar("sub_task_id", default="")


class TaskLines:
  """Where the lines of the queued task the code runs in are written. A quiet task's are held until it ends:
  written if it fails, dropped with it if it succeeds."""

  def __init__(self, quiet: bool):
    self.quiet = quiet
    self.held: list[Callable[[], None]] = []

  def write(self, write: Callable[[], None]):
    if self.quiet:
      self.held.append(write)
    else:
      write()

  def release(self):
    """The task failed: write what was held."""
    held, self.held = self.held, []
    for write in held:
      write()


# Set while a queued task runs, and nowhere else: a request's own lines are not a task's.
task_lines: ContextVar[TaskLines | None] = ContextVar("task_lines", default=None)

fastapi_exclude_log_paths = [
    "/api",  # Healthcheck
    "/api/vector/logs",  # Logs ingestion
    "/api/telegraf/report",  # Worker reports, every few seconds from each worker
    "/api/workers/*/logs/*",  # Logs query
    "/api/tasks/*/logs",  # Task logs query
    "/api/workers/*/metrics",  # Metrics query
    "/api/socket.io/",  # Socket.IO long-polling, a request every few seconds
]


def generate_task_id_token():
  return str(uuid.uuid4())[:8]


class ContextVarFilter(logging.Filter):
  def filter(self, record):
    sub = sub_task_id.get()
    record.task_id = f"{task_id.get()}/{sub}" if sub else task_id.get()
    return True


class TaskFailed(Exception):
  """Raise to mark a task as failed without printing a traceback."""
  pass


class SuppressTracebackFilter(logging.Filter):
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
  handler.addFilter(SuppressTracebackFilter())

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
