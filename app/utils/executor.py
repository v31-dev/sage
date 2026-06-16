import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial

_CPU_COUNT = max(1, os.cpu_count() or 1)

# Every thread pool in the process is defined here. The first four are the
# operation-queue lanes (wired into TaskQueue by the Manager); each scope root
# gets its own pool -- platform/common/metrics one worker each (metrics two on
# bigger hosts), app the rest.
PLATFORM_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-platform")
COMMON_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-common")
APP_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, (_CPU_COUNT * 2) - 2), thread_name_prefix="sage-app")
METRICS_EXECUTOR = ThreadPoolExecutor(
    max_workers=1 if _CPU_COUNT <= 2 else 2, thread_name_prefix="sage-metrics")

# Fire-and-forget webhook sends (Manager.notify); not a queue lane.
NOTIFICATIONS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-notifications")

# Log ingestion is a request-path write to an independent SQLite store, so it
# stays off the operation queue. Single writer to serialize the SQLite writes.
LOGS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-logs")

# Set by TaskQueue.run_task to the running task's pool, so blocking offloads
# inside an operation flow to that pool without each call site naming it.
active_executor: ContextVar[ThreadPoolExecutor | None] = ContextVar(
    "active_executor", default=None)


def run_in_executor_with_context(
    func,
    *args,
    executor: ThreadPoolExecutor | None = None,
    **kwargs,
):
  """Run a sync function in a thread pool with the current context preserved.

  Pool resolution: an explicit ``executor``, else the running task's pool
  (``active_executor``, set by TaskQueue.run_task). Raises when neither is set
  -- every offload must name a pool or run inside a queued task, so nothing
  silently lands on the event loop's default executor.
  """
  pool = executor or active_executor.get()
  if pool is None:
    name = getattr(func, "__name__", repr(func))
    raise RuntimeError(
        f"run_in_executor_with_context({name}) has no executor: pass one "
        "explicitly or call it from within a queued task.")

  ctx = copy_context()
  loop = asyncio.get_running_loop()
  return loop.run_in_executor(pool, partial(ctx.run, func, *args, **kwargs))
