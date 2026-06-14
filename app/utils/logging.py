import asyncio
import fnmatch
import inspect
import logging
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial, wraps

from rocketry import Rocketry
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
_REMOTE_EXECUTOR = ThreadPoolExecutor(
    max_workers=2 if _CPU_COUNT == 1 else min(3, _CPU_COUNT),
    thread_name_prefix="sage-remote",
)


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
  """Run sync function in an executor with current request context preserved."""
  ctx = copy_context()
  loop = asyncio.get_running_loop()
  return loop.run_in_executor(
      executor or _REMOTE_EXECUTOR,
      partial(ctx.run, func, *args, **kwargs),
  )


def with_task_id(func, log=True):
  # log=False suppresses the per-run started/completed lines for high-frequency
  # tasks (e.g. the 1s operation dispatcher); failures are always logged.
  _logger = logging.getLogger(func.__module__)
  if inspect.iscoroutinefunction(func):

    @wraps(func)
    async def wrapper(*args, **kwargs):
      # _task_id injected by LoggedSession for API-triggered runs; otherwise generate fresh.
      var_token = task_id.set(kwargs.pop("_task_id", None) or generate_task_id_token())
      try:
        if log:
          _logger.info(f"{func.__name__}: started")
        result = await func(*args, **kwargs)
        if log:
          _logger.info(f"{func.__name__}: completed")
        return result
      except Exception as exc:
        if isinstance(exc, TaskFailed):
          _logger.error(f"{func.__name__}: failed")
        else:
          _logger.exception(f"{func.__name__}: failed")
        raise
      finally:
        task_id.reset(var_token)

    return wrapper
  else:

    @wraps(func)
    def wrapper(*args, **kwargs):
      var_token = task_id.set(kwargs.pop("_task_id", None) or generate_task_id_token())
      try:
        if log:
          _logger.info(f"{func.__name__}: started")
        result = func(*args, **kwargs)
        if log:
          _logger.info(f"{func.__name__}: completed")
        return result
      except Exception as exc:
        if isinstance(exc, TaskFailed):
          _logger.error(f"{func.__name__}: failed")
        else:
          _logger.exception(f"{func.__name__}: failed")
        raise
      finally:
        task_id.reset(var_token)

    return wrapper


class LoggedSession:
  """Wraps a rocketry session so .run() auto-propagates the current request task_id."""

  def __init__(self, session):
    self._session = session

  def __getitem__(self, name):
    task_obj = self._session[name]

    class _Proxy:
      def run(_self, *args, **kwargs):
        # Pass current request task_id directly into the task via kwarg.
        # with_task_id pops it before calling the real function.
        kwargs["_task_id"] = task_id.get()
        return task_obj.run(*args, **kwargs)

    return _Proxy()

  def get_task(self, name):
    return self._session[name]

  def is_task_pending(self, name):
    task_obj = self.get_task(name)
    return task_obj.is_alive() or bool(task_obj.batches)

  def __getattr__(self, name):
    return getattr(self._session, name)


class TaskFailed(Exception):
  """Raise to mark a rocketry task as failed without printing a traceback."""
  pass


class _SuppressTracebackFilter(logging.Filter):
  """Strip exception tracebacks from rocketry task log records."""

  def filter(self, record):
    if record.exc_info and record.exc_info[0] is TaskFailed:
      record.exc_info = None
      record.exc_text = None
    return True


class LoggedRocketry(Rocketry):
  """Rocketry subclass that auto-applies with_task_id to every registered task."""

  def task(self, *args, log=True, **kwargs):
    decorator = super().task(*args, **kwargs)
    return lambda func: decorator(with_task_id(func, log=log))


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
