import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial

# Every thread pool in the process is defined here. LANES holds the operation-queue
# lanes (the Manager builds its TaskQueue from it); each scope root gets its own pool. Worker counts are fixed I/O-concurrency limits, not derived
# from os.cpu_count(): these lanes run blocking network I/O (Cloudflare API, S3,
# tailscale status, httpx), so the manager's core count doesn't gate them -- and in a CPU-limited
# container os.cpu_count() reports host cores, not the container's allowance.
# platform has no child scopes, so its tasks never run concurrently -- one worker.
# common is the low-priority lane (settings checks, log cleanup, release refresh), one
# worker too: its tasks run one at a time even where their scopes would allow more.
# app sizes for a couple of parallel multi-worker deploys plus the minutely per-app status
# sweep; metrics for concurrent per-host collection that tolerates a few slow hosts against
# the 10s collect timeout.
LANES = {
    "platform": ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-platform"),
    "common": ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-common"),
    "app": ThreadPoolExecutor(max_workers=6, thread_name_prefix="sage-app"),
    "metrics": ThreadPoolExecutor(max_workers=2, thread_name_prefix="sage-metrics"),
}

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


def submit_with_context(executor: ThreadPoolExecutor, func, *args, **kwargs):
  """Fire-and-forget sibling of run_in_executor_with_context: run func on the
  given pool with the current context preserved (so task_id flows to the worker
  thread for log correlation). Works without a running loop; returns the Future,
  which callers may ignore."""
  ctx = copy_context()
  return executor.submit(ctx.run, func, *args, **kwargs)
