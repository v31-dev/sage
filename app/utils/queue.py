import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from inspect import iscoroutinefunction
from threading import Lock
from typing import Callable

from utils.common import utcnow
from utils.executor import active_executor, run_in_executor_with_context
from utils.logging import TaskFailed, TaskLines, new_task_id, task_id, task_lines

logger = logging.getLogger(__name__)

# The scope that conflicts with every task, one holding no scope included.
EVERYTHING = "*"


def _is_ancestor_or_equal(a: str, b: str) -> bool:
  # True when scope `a` covers `b`: `*`, the same scope, or an ancestor of it in the ":"-delimited hierarchy.
  return a == EVERYTHING or b == a or b.startswith(a + ":")


def _scope_conflict(a: str, b: str) -> bool:
  # Two scopes conflict when one covers the other: "app" conflicts with
  # "app:app1"; "app:app1" conflicts with itself; "app:app1" and "app:app2"
  # are siblings and do not conflict.
  return _is_ancestor_or_equal(a, b) or _is_ancestor_or_equal(b, a)


def _scopes_conflict(a: frozenset[str], b: frozenset[str]) -> bool:
  return EVERYTHING in a or EVERYTHING in b or any(_scope_conflict(sa, sb) for sa in a for sb in b)


def _dominates(a: frozenset[str], b: frozenset[str]) -> bool:
  # True when `a` is at the same level or higher than `b`. Used for priority
  # insertion: a priority task is placed behind tasks that dominate it (other
  # platform tasks) and ahead of everything else (app tasks).
  return EVERYTHING in a or any(_is_ancestor_or_equal(sa, sb) for sa in a for sb in b)


class OnConflict(Enum):
  """How add_task admits a task when a task with the same key is pending or running.

  Admission decides whether a request queues a task. It is separate from mutual exclusion: the dispatcher always
  serializes conflicting scopes, so a queued task waits for its scopes to free. DEDUP and REPLACE compare keys,
  never scopes; QUEUE ignores the key (every request runs).
  """
  DEDUP = "dedup"      # a pending or running task with the key is the request's task; else enqueue
  QUEUE = "queue"      # always enqueue
  REPLACE = "replace"  # supersede the pending tasks with the key (latest wins), then enqueue


class QueueState(Enum):
  """Whether the dispatcher starts tasks. Paused is like one long task holding every
  scope: tasks still queue (admission is unchanged, so DEDUP/REPLACE keep recurring work
  to one pending copy) and start on resume. Restoring is paused except for the restore
  itself, which starts once the running tasks have finished (see `end_restore`). Stopping
  is final: nothing new starts or queues. In memory only, so a restart comes back
  running."""
  RUNNING = "running"
  PAUSED = "paused"
  RESTORING = "restoring"
  STOPPING = "stopping"


@dataclass(frozen=True)
class QueuedTask:
  key: tuple[str, ...]          # the operation's name, then its subject's ids and its intent
  scopes: frozenset[str]
  task: Callable
  executor: str                 # key into TaskQueue executors
  params: dict = field(default_factory=dict)  # JSON-safe arguments of `task`, recorded and shown
  inputs: dict = field(default_factory=dict)  # arguments of `task` that are never recorded or shown
  labels: dict = field(default_factory=dict)  # what people call its subjects, as they were named when it was queued
  task_id: str | None = None
  quiet: bool = False           # recorded and its lines kept only on failure (housekeeping)
  restore: bool = False         # the platform restore: the one task that starts while restoring
  queued_at: datetime = field(default_factory=utcnow)
  started_at: datetime | None = None
  lines: TaskLines | None = None  # from its start: where its lines go, and what tells this run apart

  @property
  def name(self) -> str:
    return self.key[0]


class Outcome(Enum):
  ACCEPTED = "accepted"   # queued as a new task
  EXISTING = "existing"   # a pending or running task with its key does it
  REFUSED = "refused"     # not queued: the queue is stopping, or a restore while it is not running


@dataclass(frozen=True)
class Admission:
  """What `admit` decides for a new task. Accepted: `task` is what to queue, at `position` once the `superseded` are
  removed. Existing: `task` is the pending or running one with its key, made visible when `reveal`. Refused: recorded
  as cancelled when `cancelled`."""
  outcome: Outcome
  task: QueuedTask | None = None
  superseded: tuple[QueuedTask, ...] = ()
  position: int = 0
  reveal: bool = False
  cancelled: bool = False


@dataclass(frozen=True)
class Added:
  """What add_task did with a request: the id of its task, or of the task already doing it; none when it was refused
  without a record."""
  outcome: Outcome
  task_id: str | None = None


def admit(state: QueueState, running: list[QueuedTask], queue: list[QueuedTask], new: QueuedTask,
          on_conflict: OnConflict, priority: bool) -> Admission:
  """Whether `new` is queued, done by a task with its key, or refused, and where it goes. A task with another key on a
  conflicting scope never stops it: it is queued and waits. A visible request makes the task that does it visible."""
  if new.restore and state != QueueState.RUNNING:
    return Admission(Outcome.REFUSED)
  if state == QueueState.STOPPING:
    return Admission(Outcome.REFUSED, cancelled=True)

  if on_conflict == OnConflict.DEDUP:
    # A pending task starts later than a running one, so it is the one that does what was asked.
    existing = next((task for task in (*queue, *running) if task.key == new.key), None)
    if existing is not None:
      return Admission(Outcome.EXISTING, existing, reveal=existing.quiet and not new.quiet)

  superseded = ()
  if on_conflict == OnConflict.REPLACE:
    superseded = tuple(task for task in queue if task.key == new.key)
    queue = [task for task in queue if task.key != new.key]
    if not all(task.quiet for task in superseded):
      new = replace(new, quiet=False)

  position = len(queue)
  if priority:
    # Behind the tasks that dominate it (same level or higher), ahead of the rest.
    position = 0
    for queued in queue:
      if not _dominates(queued.scopes, new.scopes):
        break
      position += 1
  return Admission(Outcome.ACCEPTED, new, superseded, position)


def next_to_start(state: QueueState, queue: list[QueuedTask], running: list[QueuedTask]) -> list[QueuedTask]:
  """The pending tasks to start now, in order: none unless running or restoring, only the restore while
  restoring, and none whose scopes conflict with a running task or with one started earlier in this scan."""
  if state not in (QueueState.RUNNING, QueueState.RESTORING):
    return []
  held = [task.scopes for task in running]
  starting = []
  for task in queue:
    if state == QueueState.RESTORING and not task.restore:
      continue
    if any(_scopes_conflict(task.scopes, scopes) for scopes in held):
      continue
    starting.append(task)
    held.append(task.scopes)
  return starting


class TaskQueue:
  """In-memory, single-dispatcher operation queue with hierarchical scope locking.

  Scopes form a ":"-delimited hierarchy. A task holding a parent scope ("app")
  conflicts with every child ("app:app1", "app:app2"); siblings never conflict,
  so operations on different applications run in parallel while a platform-wide
  task (holding the parent) excludes them all. `*` conflicts with every task.

  The dispatcher is the only place that starts tasks, so the single FIFO scan
  per tick is race-free. A threading.Lock guards the queue because producers may
  be called from FastAPI's sync-route threadpool, while the dispatcher and task
  coroutines run on the event loop.
  """

  def __init__(self, scopes: frozenset[str], executors: dict[str, ThreadPoolExecutor],
               record: Callable[["QueuedTask", str], None] | None = None,
               notify: Callable[[], None] | None = None):
    self._scopes = scopes
    self._executors = executors
    # Persistence hook called with (task, status) at each terminal state
    # (completed/failed/cancelled) so the owner can log tasks to a store.
    # Running tasks are not recorded -- they live in memory. Best-effort.
    self._record = record or (lambda *_: None)
    # Called, outside the lock and on the thread that made the change, when the running or queued tasks or the
    # queue state changed or a terminal state was recorded. It says only that something changed.
    self._notify = notify or (lambda: None)

    self._lock = Lock()
    self._state = QueueState.RUNNING
    self._queue: list[QueuedTask] = []
    self._running: list[QueuedTask] = []
    self._tasks: set[asyncio.Task] = set()

  def add_task(self, task: Callable, key: tuple[str, ...], scopes: frozenset[str], executor: str,
               params: dict | None = None, inputs: dict | None = None, labels: dict[str, str] | None = None,
               task_id: str | None = None, on_conflict: OnConflict = OnConflict.DEDUP,
               priority: bool = False, quiet: bool = False, restore: bool = False) -> Added:
    """Request a task: accepted (queued), existing (a pending or running task with the same key does it) or refused
    (the queue is stopping, recorded as cancelled; or a restore while the queue isn't running).

    ### Parameters
    - task: the callable to run (sync or async).
    - key: the operation's name, then its subject's ids and its intent, e.g. `("deploy", container_id)`: what
      DEDUP and REPLACE compare. Its first part is the task's name.
    - scopes: scopes this task needs exclusive access to. A parent scope ("app")
      conflicts with all children ("app:app1"); siblings never conflict; `*`
      conflicts with every task. Each scope's root must be one of the roots passed
      to the constructor.
    - executor: name of the executor (key in the constructor's executors dict).
    - params: JSON-safe keyword arguments of `task` (ids and flags), recorded and shown.
    - inputs: keyword arguments of `task` that are never recorded or shown (a frozen
      snapshot, a storage session).
    - labels: what people call the task's subjects, e.g. `{"Application": "shop / web"}`,
      recorded and shown.
    - task_id: log-correlation id; generated here when omitted (scheduled work).
    - on_conflict: admission policy (see OnConflict).
    - priority: insert ahead of lower/unrelated tasks but behind tasks that
      dominate it (same level or higher) -- a platform task jumps over app
      tasks but not over other platform tasks.
    - quiet: record this task and keep its lines only when it fails, so
      high-frequency housekeeping (e.g. metrics) does not flood the record. A
      visible request with the same key makes it visible.
    - restore: this is the platform restore, which holds `*`. Adding it puts the
      queue in the restoring state, where only it starts (once the running tasks
      have finished); the task ends that state with `end_restore`.
    """
    scopes = frozenset({EVERYTHING}) if EVERYTHING in scopes else frozenset(scopes)
    new_task = QueuedTask(key=tuple(key), scopes=scopes, task=task, executor=executor, params=params or {},
                          inputs=inputs or {}, labels=labels or {}, task_id=task_id or new_task_id(), quiet=quiet,
                          restore=restore)
    self._validate(new_task)
    with self._lock:
      admission = admit(self._state, self._running, self._queue, new_task, on_conflict, priority)
      if admission.outcome == Outcome.ACCEPTED:
        for superseded in admission.superseded:
          self._queue.remove(superseded)
        self._queue.insert(admission.position, admission.task)
        if restore:
          self._state = QueueState.RESTORING
          logger.info("Restoring: nothing else starts until the restore has finished.")
      elif admission.reveal:
        for tasks in (self._queue, self._running):
          tasks[:] = [replace(entry, quiet=False) if entry is admission.task else entry for entry in tasks]

    # Persist cancellations outside the lock: _record does a DB write, which must
    # stay off the critical section so the event loop never blocks on it.
    cancelled = [new_task] if admission.cancelled else list(admission.superseded)
    self._record_cancelled(cancelled)
    if admission.reveal and admission.task.lines is not None:
      admission.task.lines.release()
    if admission.outcome == Outcome.ACCEPTED or cancelled or admission.reveal:
      self._emit()
    if admission.outcome == Outcome.REFUSED:
      return Added(Outcome.REFUSED, new_task.task_id if admission.cancelled else None)
    return Added(admission.outcome, admission.task.task_id)

  def is_busy(self, scopes: frozenset[str]) -> bool:
    """True if any running or pending task conflicts with the given scopes."""
    scopes = frozenset(scopes)
    with self._lock:
      return any(_scopes_conflict(scopes, task.scopes) for task in (*self._running, *self._queue))

  def has_task(self, prefix: tuple[str, ...], running_only: bool = False) -> bool:
    """True if a task whose key starts with `prefix` is running, or pending unless `running_only`: `("sync_domain",
    domain_id)` for that domain's sync, `("backup_database",)` for any platform backup."""
    prefix = tuple(prefix)
    with self._lock:
      tasks = self._running if running_only else (*self._running, *self._queue)
      return any(task.key[:len(prefix)] == prefix for task in tasks)

  @property
  def state(self) -> QueueState:
    return self._state

  def pause(self):
    """Start no new tasks until resume(); running tasks finish. No-op unless running."""
    with self._lock:
      if self._state != QueueState.RUNNING:
        return
      self._state = QueueState.PAUSED
    logger.info("Paused: no new tasks start until resumed.")
    self._emit()

  def resume(self):
    with self._lock:
      if self._state != QueueState.PAUSED:
        return
      self._state = QueueState.RUNNING
    logger.info("Resumed.")
    self._emit()

  def end_restore(self, restored: bool, follow_ups: list[dict] = ()):
    """Leave the restoring state, called by the restore task when it ends.

    After a restore that took effect, the tasks still waiting were queued against the
    previous data, so they are cancelled, and `follow_ups` (add_task arguments) are
    queued ahead of anything added since. After a restore that failed or was rolled
    back nothing changed, and the waiting tasks run as usual."""
    with self._lock:
      cancelled = []
      if restored and self._state == QueueState.RESTORING:
        cancelled = list(self._queue)
        self._queue.clear()
    self._record_cancelled(cancelled)

    # Still restoring here, so nothing starts before the follow-ups are queued.
    if restored:
      for follow_up in follow_ups:
        self.add_task(**follow_up, priority=True)

    with self._lock:
      if self._state == QueueState.RESTORING:
        self._state = QueueState.RUNNING
    logger.info("Restore finished.")
    self._emit()

  def stop(self):
    """Stop for good when Sage exits: pending tasks, and any added later, are recorded
    as cancelled instead of silently lost. Running tasks are left to finish."""
    with self._lock:
      self._state = QueueState.STOPPING
      cancelled = list(self._queue)
      self._queue.clear()
    self._record_cancelled(cancelled)
    self._emit()

  def snapshot(self) -> dict:
    """Point-in-time view of running and queued tasks."""
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
        "key": list(task.key),
        "labels": task.labels,
        "scopes": sorted(task.scopes),
        "params": task.params,
        "executor": task.executor,
        "quiet": task.quiet,
        "status": status,
        "queued_at": task.queued_at,
        "started_at": task.started_at,
    }

  def _validate(self, task: QueuedTask):
    # Fail fast on wiring mistakes. Scope roots are dynamic below the root
    # (e.g. "app:app1"), so only the root segment is checked.
    if not task.key or not all(isinstance(part, str) and part for part in task.key):
      raise ValueError(f"A task key is a tuple of names, not {task.key!r}")
    for scope in task.scopes:
      root = scope.split(":", 1)[0]
      if scope != EVERYTHING and root not in self._scopes:
        raise ValueError(f"Unknown scope root '{root}' (allowed: {sorted(self._scopes)})")
    if task.executor not in self._executors:
      raise ValueError(f"Unknown executor '{task.executor}' (allowed: {sorted(self._executors)})")
    if task.restore and task.scopes != {EVERYTHING}:
      raise ValueError("The restore holds '*'")
    if task.params.keys() & task.inputs.keys():
      raise ValueError(f"An argument is both a param and an input: {sorted(task.params.keys() & task.inputs.keys())}")
    try:
      json.dumps(task.params)
    except (TypeError, ValueError) as e:
      raise ValueError(f"Task params are recorded, so they must be JSON: {e}") from None
    if not all(isinstance(name, str) and isinstance(label, str) for name, label in task.labels.items()):
      raise ValueError(f"Task labels name each subject in text, not {task.labels!r}")

  def dispatch_tick(self):
    """Start every pending task whose scopes do not conflict with running work."""
    with self._lock:
      starting = next_to_start(self._state, self._queue, self._running)
      for task in starting:
        self._queue.remove(task)
        task = replace(task, started_at=utcnow(), lines=TaskLines(quiet=task.quiet))
        self._running.append(task)
        handle = asyncio.create_task(self.run_task(task))
        self._tasks.add(handle)
        handle.add_done_callback(self._tasks.discard)
    if starting:
      self._emit()

  def _emit(self):
    try:
      self._notify()
    except Exception:
      logger.exception("The queue's change hook failed.")

  def _record_cancelled(self, tasks: list[QueuedTask]):
    """Log and persist already-removed pending tasks as cancelled. Call this
    OUTSIDE the lock -- `_record` writes to the DB, which must not run inside the
    critical section. Callers (admission, end_restore, stop()) remove
    the tasks from the queue under the lock, then hand them here."""
    for task in tasks:
      logger.info(f"{task.name}: cancelled")
      if not task.quiet:
        self._record(task, "cancelled")

  async def run_task(self, task: QueuedTask):
    token = task_id.set(task.task_id)
    lines_token = task_lines.set(task.lines)
    # Bind the task's pool so blocking offloads inside it (and the sync task
    # itself) flow to that executor without each call site naming it.
    executor_token = active_executor.set(self._executors[task.executor])
    status = "failed"
    try:
      logger.info(f"{task.name}: started")
      if iscoroutinefunction(task.task):
        await task.task(**task.params, **task.inputs)
      else:
        await run_in_executor_with_context(task.task, **task.params, **task.inputs)
      logger.info(f"{task.name}: completed")
      status = "completed"
    except TaskFailed:
      logger.error(f"{task.name}: failed")
    except asyncio.CancelledError:
      # Loop shutdown mid-task. Records as failed
      logger.warning(f"{task.name}: failed (interrupted by shutdown)")
      raise
    except Exception:
      logger.exception(f"{task.name}: failed")
    finally:
      if status == "failed":
        task.lines.release()
      active_executor.reset(executor_token)
      task_lines.reset(lines_token)
      task_id.reset(token)
      with self._lock:
        # A visible request may have replaced this run's entry; its lines are what this run alone has.
        current = next(entry for entry in self._running if entry.lines is task.lines)
        self._running.remove(current)
      # Quiet reconcilers (metrics, per-app syncs) skip routine completion so
      # they don't flood the store; failures and cancellations always record.
      if status == "failed" or not current.quiet:
        self._record(current, status)
      self._emit()
