import logging
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable
from contextvars import ContextVar

from starlette.routing import compile_path

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


def new_task_id():
  return str(uuid.uuid4())[:8]


class TaskIdFilter(logging.Filter):
  """Gives each record the id of the task it is logged in, `task/part` inside a container's part, for the stdout
  format."""

  def filter(self, record):
    sub = sub_task_id.get()
    record.task_id = f"{task_id.get()}/{sub}" if sub else task_id.get()
    return True


class TaskFailed(Exception):
  """Raise to mark a task as failed without printing a traceback."""
  pass


# The mark of a route whose requests the access log leaves out, one asked every few seconds or more:
# `@router.get(path, openapi_extra=NO_ACCESS_LOG)`.
NO_ACCESS_LOG = {"x-no-access-log": True}


def unlogged_paths(apps: Iterable, prefixes: Iterable[str] = ()) -> list[re.Pattern]:
  """The paths the access log leaves out: those of the routes of `apps` marked with `NO_ACCESS_LOG`, read from the
  apps' OpenAPI description (which lists the routes of every router they include), and those under each of
  `prefixes`."""
  marked = [path for app in apps for path, operations in app.openapi()["paths"].items()
            if any(operation.get("x-no-access-log") for operation in operations.values())]
  return ([compile_path(path)[0] for path in marked]
          + [re.compile(f"^{re.escape(prefix)}(/.*)?$") for prefix in prefixes])


class AccessLogFilter(logging.Filter):
  """Leaves out of uvicorn's access log the requests whose path is one of `paths`."""

  def __init__(self, paths: list[re.Pattern]):
    super().__init__()
    self.paths = paths

  def filter(self, record):
    # uvicorn's access line: the client, the method, the path with its query, the HTTP version and the status.
    if not isinstance(record.args, tuple) or len(record.args) < 3:
      return True
    path = str(record.args[2]).partition("?")[0]
    return not any(pattern.match(path) for pattern in self.paths)


def get_log_level() -> int:
  name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
  return logging.getLevelNamesMapping().get(name, logging.INFO)


def setup_logger(unlogged: list[re.Pattern]):
  """Every line to stdout, with its task's id; the access log without the requests to the `unlogged` paths."""
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(logging.Formatter(LOG_FORMAT))
  # Filter on the handler runs once for every record it processes,
  # covering propagated child-logger records that bypass parent logger filters.
  handler.addFilter(TaskIdFilter())

  root_logger = logging.getLogger()
  root_logger.setLevel(get_log_level())
  root_logger.handlers.clear()
  root_logger.addHandler(handler)

  logging.getLogger("httpx").setLevel(logging.WARNING)
  logging.getLogger("asyncssh").setLevel(logging.WARNING)
  # APScheduler logs every job run at INFO; keep only warnings/errors (missed
  # runs, job exceptions) so the 1s dispatch tick doesn't flood the logs.
  logging.getLogger("apscheduler").setLevel(logging.WARNING)
  logging.getLogger("uvicorn.access").addFilter(AccessLogFilter(unlogged))
