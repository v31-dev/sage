import asyncio
import itertools
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Awaitable, Callable

from fastapi.encoders import jsonable_encoder

import config
from services.base import Base
from utils.executor import UPDATES_EXECUTOR, run_in_executor_with_context

logger = logging.getLogger(__name__)


@unique
class UpdateSource(StrEnum):
  """What can change a topic. An owner of state says that it changed, and fires only when what it owns differs from before."""
  TASKS = "tasks"                    # the running and queued tasks
  QUEUE_STATE = "queue_state"        # running, paused, restoring or stopping
  RESTORING = "restoring"            # the backup being restored
  SETTINGS_APPLYING = "settings_applying"  # the task that makes each settings section apply
  BACKUP_STATUS = "backup_status"    # whether a platform backup runs or backups are being discovered
  WORKER_LIST = "worker_list"        # each worker's status, path and container count
  CHECKS = "checks"                  # the result of checking a settings section
  LATEST_VERSION = "latest_version"  # the newest version released
  RELEASE = "release"                # a release was fetched


@unique
class UpdateTopic(StrEnum):
  """The names of the state topics a connection can follow."""
  INFO = "info"
  TASKS = "tasks"
  BACKUP_STATUS = "backup-status"
  RELEASE = "release"
  WORKERS = "workers"


@unique
class UpdateFeed(StrEnum):
  """The names of the feeds a connection can follow. A feed's owner sends each new row to it."""
  NOTIFICATIONS = "notifications"
  TASK_HISTORY = "task-history"


@dataclass(frozen=True)
class Topic:
  load: Callable[[], object]  # the state, as the REST route for it returns it
  sources: frozenset[UpdateSource]  # the sources whose change makes the state worth sending again


@dataclass(frozen=True)
class Feed:
  """A list of rows that grows, sent as its owner publishes them."""


_UNSET = object()


class Fact:
  """A value an owner works out from something that changes (the queue, say). `check()` works it out and, when it
  differs from the last time, calls `on_change` with it and fires its source. Safe to call from any thread: the
  value is worked out and handed to `on_change` under a lock, so the values arrive in the order they were worked out."""

  def __init__(self, compute: Callable[[], object], source: "UpdateSource | None" = None,
               on_change: Callable[[object], None] | None = None):
    self._compute = compute
    self._source = source
    self._on_change = on_change
    self._lock = threading.Lock()
    self.last: object = _UNSET

  def check(self):
    with self._lock:
      value = self._compute()
      differs = value != self.last
      self.last = value
      if differs and self._on_change is not None:
        self._on_change(value)
    if differs and self._source is not None:
      Updates().changed(self._source)


Emit = Callable[[str, dict], Awaitable[None]]


def state_frame(topic: str, data: object) -> tuple[str, dict]:
  return "state", {"topic": topic, "data": data}


def error_frame(topic: str, status: int) -> tuple[str, dict]:
  return "topic_error", {"topic": topic, "status": status}


class Connection:
  """One browser's Socket.IO connection: the topics it follows, and the frames waiting to be sent."""

  def __init__(self, emit: Emit):
    self.emit = emit
    self.topics: set[str] = set()
    self._pending: dict[str | int, tuple[str, dict]] = {}
    self._sequence = itertools.count()
    self._wake = asyncio.Event()
    self._writer = asyncio.get_running_loop().create_task(self._write())

  def offer(self, key: str, frame: tuple[str, dict]):
    """Queue a frame, in place of one for the same key that has not been sent."""
    self._pending[key] = frame
    self._wake.set()

  def append(self, frame: tuple[str, dict]):
    """Queue a frame behind the ones waiting, replacing none."""
    self._pending[next(self._sequence)] = frame
    self._wake.set()

  async def _write(self):
    try:
      while True:
        await self._wake.wait()
        self._wake.clear()
        batch, self._pending = self._pending, {}
        for event, payload in batch.values():
          await self.emit(event, payload)
    except Exception:
      logger.debug("A connection could no longer be written to.", exc_info=True)

  def forget(self, topic: str):
    for key in [key for key, (_, payload) in self._pending.items() if payload["topic"] == topic]:
      del self._pending[key]

  def close(self):
    self._writer.cancel()


class Updates(Base):
  """Pushes to Socket.IO connections. A topic is a resource: a connection that follows it is sent its whole
  state each time it changes, and nothing when it subscribes; a page loads the first copy from the REST route for the
  topic. A change is only a source saying it changed (`changed`, from any thread); when the wait that it starts has
  passed, each topic that is followed and depends on a source that changed is computed again and sent. A feed is a
  list of rows that grows: each row `publish`ed to it is sent to the connections that follow it."""

  def __init__(self):
    super().__init__()
    self._topics: dict[str, Topic | Feed] = {}
    self._connections: dict[str, Connection] = {}
    self._subscribers: dict[str, set[Connection]] = {}
    self._running: set[str] = set()
    self._rerun: set[str] = set()
    self._changed: set[UpdateSource] = set()
    self._timer = None
    self._loop = None
    self._tasks: set[asyncio.Task] = set()

  def connect(self, sid: str, emit: Emit, topics: dict[UpdateTopic | UpdateFeed, Topic | Feed]):
    self._loop = asyncio.get_running_loop()
    self._topics = topics
    self._connections[sid] = Connection(emit)

  def disconnect(self, sid: str):
    connection = self._connections.pop(sid, None)
    if connection is None:
      return
    for topic in connection.topics:
      self._subscribers.get(topic, set()).discard(connection)
    connection.close()

  def subscribe(self, sid: str, topic: str):
    connection = self._connections.get(sid)
    if connection is None:
      return
    if topic not in self._topics:
      connection.offer(topic, error_frame(topic, 404))
    else:
      connection.topics.add(topic)
      self._subscribers.setdefault(topic, set()).add(connection)

  def unsubscribe(self, sid: str, topic: str):
    connection = self._connections.get(sid)
    if connection is None:
      return
    connection.topics.discard(topic)
    self._subscribers.get(topic, set()).discard(connection)
    connection.forget(topic)

  def changed(self, source: UpdateSource):
    """A source changed. Safe to call from any thread."""
    loop = self._loop
    if loop is None:
      return
    try:
      loop.call_soon_threadsafe(self._add_source, source)
    except RuntimeError:
      logger.debug("A change was dropped: the event loop is closed.")

  def publish(self, feed: UpdateFeed, rows: list):
    """Send rows to the subscribers of a feed. Safe to call from any thread."""
    loop = self._loop
    if loop is None:
      return
    frame = state_frame(feed, jsonable_encoder(rows))
    try:
      loop.call_soon_threadsafe(self._send, feed, frame)
    except RuntimeError:
      logger.debug("Rows were dropped: the event loop is closed.")

  def _send(self, feed: str, frame: tuple[str, dict]):
    for connection in self._subscribers.get(feed, set()):
      connection.append(frame)

  def _add_source(self, source: UpdateSource):
    self._changed.add(source)
    if self._timer is None:
      self._timer = self._loop.call_later(config.UPDATES_COALESCE_SECONDS, self._flush)

  def _flush(self):
    self._timer = None
    sources, self._changed = self._changed, set()
    for name, topic in self._topics.items():
      if isinstance(topic, Topic) and self._subscribers.get(name) and topic.sources & sources:
        task = self._loop.create_task(self._refresh(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

  async def _refresh(self, name: str):
    """Compute the topic and send it to who follows it. One at a time per topic, so an older state never overtakes a
    newer one."""
    if name in self._running:
      self._rerun.add(name)
      return
    self._running.add(name)
    try:
      topic = self._topics[name]
      while True:
        self._rerun.discard(name)
        try:
          data = await run_in_executor_with_context(lambda: jsonable_encoder(topic.load()), executor=UPDATES_EXECUTOR)
        except Exception:
          logger.warning(f"Could not compute the '{name}' topic.", exc_info=True)
        else:
          for connection in self._subscribers.get(name, set()):
            connection.offer(name, state_frame(name, data))
        if name not in self._rerun:
          return
    finally:
      self._running.discard(name)
