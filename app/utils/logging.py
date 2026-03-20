import sys
import uuid
import inspect
import logging
import asyncio
import re
import fnmatch
from contextvars import ContextVar, copy_context
from functools import wraps
from rocketry import Rocketry
from starlette.middleware.base import BaseHTTPMiddleware


LOG_FORMAT = '[%(asctime)s] [%(levelname)-8s] [%(name)-19s] [%(task_id)s] %(message)s'
task_id = ContextVar('task_id', default='')

fastapi_exclude_log_paths = [
  '/api', # Healthcheck
  '/api/vector/logs', # Logs ingeestion
  '/api/workers/*/logs/*', # Logs query
  '/api/workers/*/metrics', # Metrics query
]

class ContextVarFilter(logging.Filter):
  def filter(self, record):
    record.task_id = task_id.get() or ''
    return True

uvicorn_access_logger = logging.getLogger("uvicorn.access")

class fastapi_middleware(BaseHTTPMiddleware):
  async def dispatch(self, request, call_next):
    token = str(uuid.uuid4())[:8]

    task_id.set(token)
    request.state.task_id = token

    client = f"{request.client.host}:{request.client.port}" if request.client else "-"
    full_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    uvicorn_access_logger.info(f'{client} - "{request.method} {full_path} HTTP/{request.scope["http_version"]}" -')
    response = await call_next(request)
    response.headers["X-Task-ID"] = token
    return response

def run_in_executor_with_context(func, *args):
  """Run sync function in executor with current request context preserved."""
  ctx = copy_context()
  loop = asyncio.get_running_loop()
  return loop.run_in_executor(None, ctx.run, func, *args)

def with_task_id(func):
  _logger = logging.getLogger(func.__module__)
  if inspect.iscoroutinefunction(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
      # _task_id injected by LoggedSession for API-triggered runs; otherwise generate fresh.
      var_token = task_id.set(kwargs.pop("_task_id", None) or str(uuid.uuid4())[:8])
      try:
        _logger.info(f"{func.__name__}: started")
        result = await func(*args, **kwargs)
        _logger.info(f"{func.__name__}: completed")
        return result
      except Exception:
        _logger.info(f"{func.__name__}: failed")
        raise
      finally:
        task_id.reset(var_token)
    return wrapper
  else:
    @wraps(func)
    def wrapper(*args, **kwargs):
      var_token = task_id.set(kwargs.pop("_task_id", None) or str(uuid.uuid4())[:8])
      try:
        _logger.info(f"{func.__name__}: started")
        result = func(*args, **kwargs)
        _logger.info(f"{func.__name__}: completed")
        return result
      except Exception:
        _logger.info(f"{func.__name__}: failed")
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
  def task(self, *args, **kwargs):
    decorator = super().task(*args, **kwargs)
    return lambda func: decorator(with_task_id(func))

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
        base_path = raw_path.split('?')[0]
        # Check each pattern against the path
        for pattern in self.paths:
          if fnmatch.fnmatch(base_path, pattern):
            return False
    except (AttributeError, IndexError):
      # Fallback for logs that don't match the expected uvicorn format
      pass
    return True

def setup_logger():
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(logging.Formatter(LOG_FORMAT))
  # Filter on the handler runs once for every record it processes,
  # covering propagated child-logger records that bypass parent logger filters.
  handler.addFilter(ContextVarFilter())
  handler.addFilter(_SuppressTracebackFilter())
  
  root_logger = logging.getLogger()
  root_logger.setLevel(logging.INFO)
  root_logger.handlers.clear()
  root_logger.addHandler(handler)

  logging.getLogger('httpx').setLevel(logging.WARNING)
  # Exclude some paths from logs
  logging.getLogger("uvicorn.access").addFilter(ExactPathFilter(fastapi_exclude_log_paths))

  for logger_name in ['rocketry.task', 'rocketry.scheduler', 'uvicorn', 'uvicorn.access', 'uvicorn.error']:
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.propagate = True