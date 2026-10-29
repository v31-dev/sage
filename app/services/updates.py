import asyncio
import itertools
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Awaitable, Callable

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

import config
from services.base import Base
from utils.executor import UPDATES_EXECUTOR, run_in_executor_with_context

logger = logging.getLogger(__name__)


@unique
class UpdateSource(StrEnum):
  """What can change a topic. An owner of state says that it changed, and fires only when what it owns differs from before.
  A source of one resource of a parameterised topic is keyed by that topic's argument and changes only the topic of that key."""
  TASKS = "tasks"                    # the running and queued tasks
  QUEUE_STATE = "queue_state"        # running, paused, restoring or stopping
  RESTORING = "restoring"            # the backup being restored
  SETTINGS_APPLYING = "settings_applying"  # the task that makes each settings section apply
  BACKUP_STATUS = "backup_status"    # whether a platform backup runs or backups are being discovered
  WORKER_LIST = "worker_list"        # each worker's status, path and container count
  CHECKS = "checks"                  # the result of checking a settings section
  LATEST_VERSION = "latest_version"  # the newest version released
  RELEASE = "release"                # a release was fetched
  APPLICATION = "application"        # keyed by `<project>/<name>`: the application, or a container, domain or volume of it, was written
  WORKER_ENTRY = "worker_entry"      # keyed by hostname: the worker's entry in the worker list differs
  WORKER_CONTAINERS = "worker_containers"  # keyed by hostname: a container of the worker, or what its page shows of its application, was written


@unique
class UpdateTopic(StrEnum):
  """The names of the state topics a connection can follow."""
  INFO = "info"
  TASKS = "tasks"
  BACKUP_STATUS = "backup-status"
  RELEASE = "release"
  WORKERS = "workers"
  WORKER = "worker"            # one worker: `worker:<hostname>`
  APPLICATION = "application"  # one application: `application:<project>/<name>`


@unique
class UpdateFeed(StrEnum):
  """The names of the feeds a connection can follow. A feed's owner sends each new row to it."""
  NOTIFICATIONS = "notifications"
  TASK_HISTORY = "task-history"


@dataclass(frozen=True)
class Topic:
  load: Callable[..., object]  # the state, as the REST route for it returns it; given the argument of a parameterised topic
  sources: frozenset[UpdateSource]  # the sources whose change makes the state worth sending again (keyed by the argument of a parameterised topic)
  parameterised: bool = False  # followed as `name:argument`, one topic for each argument


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
    self._changed: set[tuple[UpdateSource, str | None]] = set()
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
      self._leave(connection, topic)
    connection.close()

  def subscribe(self, sid: str, topic: str):
    connection = self._connections.get(sid)
    if connection is None:
      return
    if self._definition(topic) is None:
      connection.offer(topic, error_frame(topic, 404))
    else:
      connection.topics.add(topic)
      self._subscribers.setdefault(topic, set()).add(connection)

  def unsubscribe(self, sid: str, topic: str):
    connection = self._connections.get(sid)
    if connection is None:
      return
    connection.topics.discard(topic)
    self._leave(connection, topic)
    connection.forget(topic)

  def _leave(self, connection: Connection, topic: str):
    subscribers = self._subscribers.get(topic)
    if subscribers is not None:
      subscribers.discard(connection)
      if not subscribers:
        del self._subscribers[topic]

  def _definition(self, name: str) -> tuple[Topic | Feed, str] | None:
    """What a name that can be followed stands for, and its argument (empty for a topic with none). A parameterised
    topic is followed as `name:argument`; a name with an argument that the topic does not take, or without one that it
    does, stands for nothing."""
    key, separator, argument = name.partition(":")
    definition = self._topics.get(key)
    if definition is None or (isinstance(definition, Topic) and definition.parameterised) != bool(argument) or (
            separator and not argument):
      return None
    return definition, argument

  def changed(self, source: UpdateSource, key: str | None = None):
    """A source changed, a keyed one for the resource of that key. Safe to call from any thread."""
    loop = self._loop
    if loop is None:
      return
    try:
      loop.call_soon_threadsafe(self._add_source, source, key)
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

  def _add_source(self, source: UpdateSource, key: str | None):
    self._changed.add((source, key))
    if self._timer is None:
      self._timer = self._loop.call_later(config.UPDATES_COALESCE_SECONDS, self._flush)

  def _flush(self):
    self._timer = None
    changed, self._changed = self._changed, set()
    for name in list(self._subscribers):
      topic, argument = self._definition(name)
      if isinstance(topic, Topic) and any((source, argument or None) in changed for source in topic.sources):
        task = self._loop.create_task(self._refresh(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

  async def _refresh(self, name: str):
    """Compute the topic and send it to who follows it. One at a time per topic, so an older state never overtakes a
    newer one. What the route answers instead of the state (a 404 for a resource that is gone) is sent as the topic's
    error."""
    if name in self._running:
      self._rerun.add(name)
      return
    self._running.add(name)
    try:
      topic, argument = self._definition(name)
      while True:
        self._rerun.discard(name)
        try:
          frame = state_frame(name, await run_in_executor_with_context(
              lambda: jsonable_encoder(topic.load(argument) if topic.parameterised else topic.load()), executor=UPDATES_EXECUTOR))
        except HTTPException as answer:
          frame = error_frame(name, answer.status_code)
        except Exception:
          logger.warning(f"Could not compute the '{name}' topic.", exc_info=True)
          frame = None
        if frame is not None:
          for connection in self._subscribers.get(name, set()):
            connection.offer(name, frame)
        if name not in self._rerun:
          return
    finally:
      self._running.discard(name)
