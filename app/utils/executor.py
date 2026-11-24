import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial

# Every thread pool in the process is defined here. LANES holds the operation-queue lanes (the Manager builds its
# TaskQueue from it). A lane thread is held only by blocking work (Cloudflare, S3, ACME, the tailscale CLI, httpx,
# local SQLite and files); a worker's commands are asyncssh on the event loop and hold none. Worker counts are fixed
# limits on that blocking work, not derived from os.cpu_count(), which in a CPU-limited container reports the host's.
# A task is running, holding its scopes, from its dispatch, before it has a thread, so every task that holds a scope
# runs on main, and common runs only scheduled housekeeping that holds none, one at a time; no task relies on a lane's
# size to keep it apart from another.
LANES = {
    "main": ThreadPoolExecutor(max_workers=6, thread_name_prefix="sage-main"),
    "common": ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-common"),
}

# The topics pushed over Socket.IO (`Updates`) are computed here.
UPDATES_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sage-updates")

# Fire-and-forget webhook sends (Manager.notify); not a queue lane.
NOTIFICATIONS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-notifications")

# Log ingestion and Sage's markers: a request-path write to the log files, so it stays off the operation queue. One
# thread inserts every application and request line (see `Logs._insert`); SQLite's write lock orders it with the
# cleanup's deletions.
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
