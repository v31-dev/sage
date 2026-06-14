import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from inspect import iscoroutinefunction
from threading import Lock
from typing import Callable

from utils.logging import TaskFailed, generate_task_id_token, run_in_executor_with_context, task_id

logger = logging.getLogger(__name__)


def _normalize(scope: str) -> str:
  # "app:*" is sugar for the parent scope "app" (the whole subtree).
  return scope[:-2] if scope.endswith(":*") else scope


def _is_ancestor_or_equal(a: str, b: str) -> bool:
  # True when scope `a` is at the same level as, or an ancestor of, `b` in the
  # ":"-delimited hierarchy (i.e. `a` covers `b`).
  a, b = _normalize(a), _normalize(b)
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


@dataclass(frozen=True)
class Task:
  name: str
  scopes: frozenset[str]
  task: Callable
  params: dict
  executor: str | None = None   # key into TaskQueue executors; None runs on the loop
  task_id: str | None = None


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

  def __init__(self, scopes: frozenset[str], executors: dict[str, ThreadPoolExecutor]):
    self._scopes = scopes
    self._executors = executors

    self._lock = Lock()
    self._queue: list[Task] = []
    self._running: list[Task] = []
    self._tasks: set[asyncio.Task] = set()

  def add_task(self, task: Callable, scopes: frozenset[str], executor: str,
               name: str | None = None, params: dict | None = None,
               task_id: str | None = None,
               priority: bool = False, queue: bool = False) -> bool:
    """Add a task; return False if it was dropped.

    ### Parameters
    - task: the callable to run (sync or async).
    - scopes: scopes this task needs exclusive access to. By default the task
      is rejected when any conflict (hierarchically) with a pending or running
      task. A parent scope ("app") conflicts with all children ("app:app1").
      Each scope's root must be one of the roots passed to the constructor.
    - executor: name of the executor (key in the constructor's executors dict).
    - name: log/UI label; defaults to the task callable's name.
    - params: keyword arguments passed to `task`; defaults to none.
    - task_id: log-correlation id; generated here when omitted (scheduled work).
    - priority: insert ahead of lower/unrelated tasks but behind tasks that
      dominate it (same level or higher) -- a platform task jumps over app
      tasks but not over other platform tasks.
    - queue: if True, queue the task to wait even when its scopes conflict,
      instead of rejecting it (used for per-object housekeeping like a single
      application's status sync).
    """
    scopes = frozenset(scopes)
    self._validate(scopes, executor)
    task_id = task_id or generate_task_id_token()
    with self._lock:
      if not queue and self.has_task_scopes(scopes, only_running=False):
        return False

      new_task = Task(name=name or task.__name__, scopes=scopes, task=task,
                      params=params or {}, executor=executor, task_id=task_id)

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
      return True

  def get_task_scopes(self) -> frozenset[str]:
    """The set of all scopes currently held by pending or running tasks."""
    with self._lock:
      busy = set()
      for task in (*self._running, *self._queue):
        busy |= task.scopes
      return frozenset(busy)

  def has_task_scopes(self, scopes: frozenset[str], only_running: bool = True) -> bool:
    """True if a running (and, when only_running is False, also pending) task
    conflicts with any of the given scopes. Caller must hold self._lock."""
    if any(_scopes_conflict(scopes, task.scopes) for task in self._running):
      return True
    if not only_running:
      return any(_scopes_conflict(scopes, task.scopes) for task in self._queue)
    return False

  def is_active(self) -> bool:
    """True when any task is running or queued."""
    with self._lock:
      return bool(self._running or self._queue)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    """True if any running or pending task conflicts with the given scopes."""
    with self._lock:
      return self.has_task_scopes(frozenset(scopes), only_running=False)

  def _validate(self, scopes: frozenset[str], executor: str):
    # Fail fast on wiring mistakes: every scope's root and the executor name
    # must be declared on the queue. Scope roots are dynamic below the root
    # (e.g. "app:app1"), so only the root segment is checked.
    for scope in scopes:
      root = _normalize(scope).split(":", 1)[0]
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

  async def run_task(self, task: Task):
    token = task_id.set(task.task_id)
    try:
      logger.info(f"{task.name}: started")
      if iscoroutinefunction(task.task):
        await task.task(**task.params)
      else:
        await run_in_executor_with_context(
            task.task,
            executor=self._executors[task.executor],
            **task.params,
        )
      logger.info(f"{task.name}: completed")
    except TaskFailed:
      logger.error(f"{task.name}: failed")
    except Exception:
      logger.exception(f"{task.name}: failed")
    finally:
      task_id.reset(token)
      with self._lock:
        self._running.remove(task)
