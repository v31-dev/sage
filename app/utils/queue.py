import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from inspect import iscoroutinefunction
from threading import Lock
from typing import Callable

from utils.executor import active_executor, run_in_executor_with_context
from utils.logging import TaskFailed, generate_task_id_token, task_id

logger = logging.getLogger(__name__)


def _is_ancestor_or_equal(a: str, b: str) -> bool:
  # True when scope `a` is at the same level as, or an ancestor of, `b` in the
  # ":"-delimited hierarchy (i.e. `a` covers `b`).
  return b == a or b.startswith(a + ":")


def _scope_conflict(a: str, b: str) -> bool:
  # Two scopes conflict when one covers the other: "app" conflicts with
  # "app:app1"; "app:app1" conflicts with itself; "app:app1" and "app:app2"
  # are siblings and do not conflict.
  return _is_ancestor_or_equal(a, b) or _is_ancestor_or_equal(b, a)


def _scopes_conflict(a: frozenset[str], b: frozenset[str]) -> bool:
  return any(_scope_conflict(sa, sb) for sa in a for sb in b)


def _dominates(a: frozenset[str], b: frozenset[str]) -> bool:
  # True when `a` is at the same level or higher than `b`. Used for priority
  # insertion: a priority task is placed behind tasks that dominate it (other
  # platform tasks) and ahead of everything else (app tasks).
  return any(_is_ancestor_or_equal(sa, sb) for sa in a for sb in b)


class OnConflict(Enum):
  """How add_task admits a task when an identical or conflicting task exists.

  Admission decides *drop vs. enqueue*. It is separate from mutual exclusion:
  the dispatcher always serializes conflicting scopes regardless of mode, so a
  task that is enqueued simply waits for its scope to free.

  Identity for dedupe is name + exact scope (params are not part of it). DEDUP
  and REPLACE act on that identity; QUEUE ignores it (every call runs).
  """
  DEDUP = "dedup"      # skip if an identical op (name + scope) is pending/running; else enqueue
  QUEUE = "queue"      # always enqueue; wait for the scope to free (no dedupe)
  REPLACE = "replace"  # cancel a pending duplicate (latest wins); then enqueue


@dataclass(frozen=True)
class QueuedTask:
  name: str
  scopes: frozenset[str]
  task: Callable
  params: dict
  executor: str                 # key into TaskQueue executors
  task_id: str | None = None
  quiet: bool = False           # persist only on failure (high-frequency housekeeping)


class TaskQueue:
  """In-memory, single-dispatcher operation queue with hierarchical scope locking.

  Scopes form a ":"-delimited hierarchy. A task holding a parent scope ("app")
  conflicts with every child ("app:app1", "app:app2"); siblings never conflict,
  so operations on different applications run in parallel while a platform-wide
  task (holding the parent) excludes them all.

  The dispatcher is the only place that starts tasks, so the single FIFO scan
  per tick is race-free. A threading.Lock guards the queue because producers may
  be called from FastAPI's sync-route threadpool, while the dispatcher and task
  coroutines run on the event loop.
  """

  def __init__(self, scopes: frozenset[str], executors: dict[str, ThreadPoolExecutor],
               record: Callable[["QueuedTask", str], None] | None = None):
    self._scopes = scopes
    self._executors = executors
    # Persistence hook called with (task, status) at each terminal state
    # (completed/failed/cancelled) so the owner can log tasks to a store.
    # Running tasks are not recorded -- they live in memory. Best-effort.
    self._record = record or (lambda *_: None)

    self._lock = Lock()
    self._queue: list[QueuedTask] = []
    self._running: list[QueuedTask] = []
    self._tasks: set[asyncio.Task] = set()

  def add_task(self, task: Callable, scopes: frozenset[str], executor: str,
               name: str | None = None, params: dict | None = None,
               task_id: str | None = None,
               on_conflict: OnConflict = OnConflict.DEDUP,
               priority: bool = False, quiet: bool = False) -> bool:
    """Add a task; return False if it was dropped (DEDUP when an identical op is
    already pending or running).

    ### Parameters
    - task: the callable to run (sync or async).
    - scopes: scopes this task needs exclusive access to. A parent scope ("app")
      conflicts with all children ("app:app1"); siblings never conflict. Each
      scope's root must be one of the roots passed to the constructor.
    - executor: name of the executor (key in the constructor's executors dict).
    - name: log/UI label; defaults to the task callable's name. Also the identity
      (with the exact scope) DEDUP/REPLACE use to detect a duplicate.
    - params: keyword arguments passed to `task`; defaults to none.
    - task_id: log-correlation id; generated here when omitted (scheduled work).
    - on_conflict: admission policy (see OnConflict). DEDUP skips if an identical
      op (same name + exact scope) is already pending or running, else enqueues
      and waits; QUEUE always enqueues and waits; REPLACE supersedes a pending
      duplicate (same name + exact scope) then enqueues and waits.
    - priority: insert ahead of lower/unrelated tasks but behind tasks that
      dominate it (same level or higher) -- a platform task jumps over app
      tasks but not over other platform tasks.
    - quiet: persist this task to the record store only when it fails, so
      high-frequency housekeeping (e.g. metrics) does not flood it.
    """
    name = name or task.__name__
    scopes = frozenset(scopes)
    self._validate(scopes, executor)
    task_id = task_id or generate_task_id_token()
    cancelled: list[QueuedTask] = []
    with self._lock:
      if on_conflict == OnConflict.REPLACE:
        # Duplicate = same operation on the same exact scope (identity = name +
        # scope; params are not part of identity). Supersede the pending one;
        # persist the cancellation after the lock (see below).
        cancelled = [t for t in self._queue if t.name == name and t.scopes == scopes]
        for pending in cancelled:
          self._queue.remove(pending)
      elif on_conflict == OnConflict.DEDUP:
        # Skip if an identical op (name + exact scope) is already pending or
        # running -- pile-up / double-trigger prevention. A *different* op on a
        # conflicting scope is not dropped here; it enqueues and the dispatcher
        # serializes it by scope.
        if any(t.name == name and t.scopes == scopes
               for t in (*self._running, *self._queue)):
          return False
      # OnConflict.QUEUE: always enqueue and wait for the scope to free.

      new_task = QueuedTask(name=name, scopes=scopes, task=task,
                            params=params or {}, executor=executor, task_id=task_id,
                            quiet=quiet)

      if priority:
        position = 0
        for queued_task in self._queue:
          if _dominates(queued_task.scopes, scopes):
            position += 1            # stay behind same/higher tasks
          else:
            break                    # jump ahead of the rest
      else:
        position = len(self._queue)

      self._queue.insert(position, new_task)

    # Persist cancellations outside the lock: _record does a DB write, which must
    # stay off the critical section so the event loop never blocks on it.
    self._record_cancelled(cancelled)
    return True

  def has_task_scopes(self, scopes: frozenset[str], only_running: bool = True) -> bool:
    """True if a running (and, when only_running is False, also pending) task
    conflicts with any of the given scopes. Caller must hold self._lock."""
    if any(_scopes_conflict(scopes, task.scopes) for task in self._running):
      return True
    if not only_running:
      return any(_scopes_conflict(scopes, task.scopes) for task in self._queue)
    return False

  def is_busy(self, scopes: frozenset[str]) -> bool:
    """True if any running or pending task conflicts with the given scopes."""
    with self._lock:
      return self.has_task_scopes(frozenset(scopes), only_running=False)

  def is_task_running(self, task: Callable | str, scopes: frozenset[str] | None = None) -> bool:
    """True if a task with this identity is currently executing (not merely queued).
    Pass the task callable (rename-safe; its `__name__` is the identity add_task
    stores by default) or, for a task enqueued with an explicit `name=`, that
    string. When scopes is given, the running task must also conflict with them, so
    a per-resource task (e.g. app:<qualified_name>) is matched for one specific
    resource rather than by name across every resource. For UI/route status of a
    named operation, as distinct from is_busy's scope-only check."""
    name = task if isinstance(task, str) else task.__name__
    with self._lock:
      return any(
          running.name == name and (scopes is None or _scopes_conflict(scopes, running.scopes))
          for running in self._running
      )

  def cancel_all(self):
    """Cancel every pending (not yet running) task; running tasks are left to
    finish. Used before a restart so queued work is recorded as cancelled
    instead of silently lost when the process exits."""
    with self._lock:
      cancelled = list(self._queue)
      self._queue.clear()
    self._record_cancelled(cancelled)

  def snapshot(self) -> dict:
    """Point-in-time view of running and queued tasks (for the UI)."""
    with self._lock:
      return {
          "running": [self._task_dict(t, "running") for t in self._running],
          "queued": [self._task_dict(t, "queued") for t in self._queue],
      }

  @staticmethod
  def _task_dict(task: "QueuedTask", status: str) -> dict:
    return {
        "task_id": task.task_id,
        "name": task.name,
        "scopes": sorted(task.scopes),
        "params": task.params,
        "executor": task.executor,
        "status": status,
    }

  def _validate(self, scopes: frozenset[str], executor: str):
    # Fail fast on wiring mistakes: every scope's root and the executor name
    # must be declared on the queue. Scope roots are dynamic below the root
    # (e.g. "app:app1"), so only the root segment is checked.
    for scope in scopes:
      root = scope.split(":", 1)[0]
      if root not in self._scopes:
        raise ValueError(f"Unknown scope root '{root}' (allowed: {sorted(self._scopes)})")
    if executor not in self._executors:
      raise ValueError(f"Unknown executor '{executor}' (allowed: {sorted(self._executors)})")

  def dispatch_tick(self):
    """Start every pending task whose scopes do not conflict with running work."""
    with self._lock:
      counter = 0
      while counter < len(self._queue):
        task = self._queue[counter]
        # only_running=True: a dispatched task joins _running immediately, so
        # same-scope tasks later in this scan correctly see it as held.
        if self.has_task_scopes(task.scopes, only_running=True):
          counter += 1
        else:
          task = self._queue.pop(counter)
          self._running.append(task)
          handle = asyncio.create_task(self.run_task(task))
          self._tasks.add(handle)
          handle.add_done_callback(self._tasks.discard)

  def _record_cancelled(self, tasks: list[QueuedTask]):
    """Log and persist already-removed pending tasks as cancelled. Call this
    OUTSIDE the lock -- `_record` writes to the DB, which must not run inside the
    critical section. Callers (REPLACE admission, the pre-restart drain) remove
    the tasks from the queue under the lock, then hand them here."""
    for task in tasks:
      logger.info(f"{task.name}: cancelled")
      self._record(task, "cancelled")

  async def run_task(self, task: QueuedTask):
    token = task_id.set(task.task_id)
    # Bind the task's pool so blocking offloads inside it (and the sync task
    # itself) flow to that executor without each call site naming it.
    executor_token = active_executor.set(self._executors[task.executor])
    try:
      logger.info(f"{task.name}: started")
      if iscoroutinefunction(task.task):
        await task.task(**task.params)
      else:
        await run_in_executor_with_context(task.task, **task.params)
      logger.info(f"{task.name}: completed")
      # Quiet reconcilers (metrics, per-app syncs) skip routine completion so
      # they don't flood the store; failures and cancellations always record.
      if not task.quiet:
        self._record(task, "completed")
    except TaskFailed:
      logger.error(f"{task.name}: failed")
      self._record(task, "failed")
    except asyncio.CancelledError:
      # Loop shutdown mid-task. Records as failed
      logger.warning(f"{task.name}: failed (interrupted by shutdown)")
      self._record(task, "failed")
      raise
    except Exception:
      logger.exception(f"{task.name}: failed")
      self._record(task, "failed")
    finally:
      active_executor.reset(executor_token)
      task_id.reset(token)
      with self._lock:
        self._running.remove(task)
