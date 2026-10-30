import logging
import os
import queue
import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from peewee import CharField, Model, SqliteDatabase, TextField
from playhouse.sqlite_ext import FTS5Model, RowIDField, SearchField

import config
from services.base import Base
from services.db import Application, Project
from services.settings import Settings
from services.updates import Fact, UpdateSource
from utils.common import utcnow
from utils.logging import LOG_FORMAT, ContextVarFilter, SuppressTracebackFilter, sub_task_id, task_id, task_lines

logger = logging.getLogger(__name__)

_SYSTEM_LOG_CONTAINERS = {"cloudflared", "traefik", "telegraf"}
# The task logs' file, beside the container shards. An application's shard is named `project-app`, so none is this.
_TASKS = "tasks"
# A debug capture's file is named by its start time, so the newest is the one a capture on appends to.
_DEBUG_FILE = re.compile(r"sage-debug-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.log")


def _new_debug_file() -> str:
  return f"sage-debug-{utcnow():%Y-%m-%dT%H-%M-%SZ}.log"


class Logs(Base):
  """Per-container log shards (one SQLite DB each, with an FTS5 message index), the task logs (the lines the manager
  logs inside its queued tasks) and the debug capture (every line it logs, in a file, until the `debug` setting's stop
  time). A handler on the root logger hands the manager's lines to a drain thread to write."""

  def __init__(self):
    super().__init__()

    self.db_path = config.DATA_DIR
    os.makedirs(f"{self.db_path}/logs", exist_ok=True)
    self._dbs = {"logs": {}}
    # When the size of each file, and of all together, was last looked at (monotonic seconds).
    self._checked: dict[str, float] = {}
    self._total_checked = float("-inf")
    self._trim_lock = threading.Lock()

    self.debug_dir = Path(self.db_path) / "debug"
    os.makedirs(self.debug_dir, exist_ok=True)

    # The handler enqueues the lines; the drain thread writes them off the calling thread (often the event loop).
    self._queue = queue.SimpleQueue()
    # The file of the debug capture on, which the drain thread holds open.
    self.debug_file: str | None = None
    self._debug_lock = threading.Lock()
    self._debug_capture = Fact(self.debug_capture, UpdateSource.DEBUG_CAPTURE)

  def start_capture(self):
    """Write the manager's lines from now on, and resume a debug capture whose stop time is still ahead. Runs once
    the settings are loaded; the lines before it are in stdout only."""
    threading.Thread(target=self._capture_loop, name="sage-log-capture", daemon=True).start()
    logging.getLogger().addHandler(_CaptureHandler(self._queue, lambda: self.debug_file is not None))
    self.follow_debug_setting()

  def _capture_loop(self):
    file, name = None, None
    while True:
      kind, item = self._queue.get()
      try:
        if kind == "task":
          self.write_task_line(item)
        elif kind == "open":
          if file is not None:
            file.close()
          file, name = open(self.debug_dir / item, "ab"), item
        elif kind == "close":
          if file is not None:
            file.close()
          file, name = None, None
        elif kind == "line" and file is not None:
          file.write(item.encode())
          if file.tell() >= config.LOG_SHARD_MAX_BYTES:
            file.close()
            file = None
            self._debug_file_full(name)
        if file is not None and self._queue.empty():
          file.flush()
      except Exception:
        pass  # never let a write error kill the drain thread

  def _open(self, name: str) -> SqliteDatabase:
    db = SqliteDatabase(
        f"{self.db_path}/logs/{name}.db",
        pragmas={
            "journal_mode": "wal",
            "cache_size": -2048,  # 2MB — FTS index thrashing prevented; room for B-tree
            "busy_timeout": 5000,  # Wait up to 5s before returning SQLITE_BUSY.
            "synchronous": 1,  # NORMAL — commit writes faster, acceptable for logs
        },
    )
    db.connect(reuse_if_open=True)
    return db

  def get_logs_db(self, container):
    """
    1 database per container.
    The lines table stores all fields; the index is an FTS5 virtual table
    that indexes only the message text for fast full-text search.
    """
    # container is a qualified application name; reject anything that could escape
    # the logs directory when interpolated into the shard file path.
    if not re.fullmatch(r"[a-z0-9-]+", container or "") or container == _TASKS:
      raise ValueError(f"Invalid container name: {container!r}")

    with self.lock:
      if container not in self._dbs["logs"]:
        db = self._open(container)

        class BaseModel(Model):
          class Meta:
            database = db

        class ContainerLogs(BaseModel):
          hostname = CharField()
          ts = CharField()  # preserves nanosecond precision
          stream = CharField()
          message = TextField()

          class Meta:
            indexes = ((("ts",), False),)

        class ContainerLogsIndex(FTS5Model):
          rowid = RowIDField()
          message = SearchField()

          class Meta:
            database = db
            options = {"content": ContainerLogs, "content_rowid": "id"}

        db.create_tables([ContainerLogs, ContainerLogsIndex], safe=True)
        # Trigger keeps FTS index in sync incrementally — O(new rows) per insert,
        # not O(total rows) like rebuild() would be.
        db.execute_sql(
            """
          CREATE TRIGGER IF NOT EXISTS containerlogs_ai
          AFTER INSERT ON containerlogs BEGIN
            INSERT INTO containerlogsindex(rowid, message) VALUES (new.id, new.message);
          END
        """
        )
        # Keep the index clean when logs are deleted
        db.execute_sql(
            """
            CREATE TRIGGER IF NOT EXISTS containerlogs_ad
            AFTER DELETE ON containerlogs BEGIN
              INSERT INTO containerlogsindex(containerlogsindex, rowid, message)
              VALUES('delete', old.id, old.message);
            END;
        """
        )
        self._dbs["logs"][container] = {"db": db, "lines": ContainerLogs, "index": ContainerLogsIndex}

    return self._dbs["logs"][container]

  def task_logs_db(self):
    with self.lock:
      if _TASKS not in self._dbs["logs"]:
        db = self._open(_TASKS)

        class TaskLogs(Model):
          task_id = CharField()
          sub_task_id = CharField()
          ts = CharField()
          level = CharField()
          logger = CharField()
          message = TextField()

          class Meta:
            database = db
            indexes = ((("task_id", "sub_task_id"), False), (("ts",), False))

        db.create_tables([TaskLogs], safe=True)
        self._dbs["logs"][_TASKS] = {"db": db, "lines": TaskLogs, "index": None}

    return self._dbs["logs"][_TASKS]

  def _store(self, name: str):
    """The task logs or a container's shard, by its file's name."""
    return self.task_logs_db() if name == _TASKS else self.get_logs_db(name)

  def write_logs(self, container: str, entries: list):
    shard = self.get_logs_db(container)
    with shard["db"].atomic("IMMEDIATE"):
      shard["lines"].insert_many(entries).execute()
      # FTS index updated automatically via containerlogs_ai trigger

    logger.info(f"Wrote {len(entries)} log entries for container {container}.")
    self._check_sizes(container)

  def write_task_line(self, entry: dict):
    self.task_logs_db()["lines"].insert(**entry).execute()
    self._check_sizes(_TASKS)

  def query_logs(
      self,
      container: str,
      hostname: str = "",
      search: str = "",
      from_ts: str = "",
      to_ts: str = "",
  ) -> list:
    """
    If `hostname` is provided, filters to only that host's entries.
    If `search` is provided, filters via FTS5 full-text index on message.
    If `from_ts` and `to_ts` are provided, returns logs within that range.
    """
    shard = self.get_logs_db(container)
    ContainerLogs = shard["lines"]
    ContainerLogsIndex = shard["index"]

    # Build base query with FTS if searching
    if search:
      query = (
          ContainerLogs.select()
          .join(
              ContainerLogsIndex,
              on=(ContainerLogs.id == ContainerLogsIndex.rowid),
          )
          .where(ContainerLogsIndex.match(search))
      )
    else:
      query = ContainerLogs.select()

    # Apply common filters
    if hostname:
      query = query.where(ContainerLogs.hostname == hostname)
    if from_ts:
      query = query.where(ContainerLogs.ts >= from_ts)
    if to_ts:
      query = query.where(ContainerLogs.ts < to_ts)

    rows = query.order_by(ContainerLogs.ts.asc()).dicts()

    return list(rows)

  def query_task_logs(self, task_id: str, sub_task_id: str = "") -> dict:
    """A task's lines, or one container's part of it, in the order they were written: the newest LOG_QUERY_MAX_ROWS,
    and whether there were more. A part recorded under the task's own id (a delete, a volume restore) is the whole
    task."""
    TaskLogs = self.task_logs_db()["lines"]
    query = TaskLogs.select().where(TaskLogs.task_id == task_id)
    if sub_task_id and sub_task_id != task_id:
      query = query.where(TaskLogs.sub_task_id == sub_task_id)
    rows = list(query.order_by(TaskLogs.id.desc()).limit(config.LOG_QUERY_MAX_ROWS + 1).dicts())
    return {"lines": rows[:config.LOG_QUERY_MAX_ROWS][::-1], "more": len(rows) > config.LOG_QUERY_MAX_ROWS}

  def set_debug_stop(self, stop_at: str | None):
    """Start a debug capture in a new file until `stop_at`, move the stop time of the one on, or stop it (None)."""
    if stop_at is not None:
      when = datetime.fromisoformat(stop_at)
      if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
      if when <= utcnow():
        raise ValueError("The stop time has passed.")
      stop_at = when.isoformat()
    with self._debug_lock:
      Settings().set("debug", {"stop_at": stop_at})
      if stop_at is not None and self.debug_file is None:
        self._open_debug_file(_new_debug_file())
      elif stop_at is not None:
        logger.info(f"Debug capture on until {stop_at} UTC: {self.debug_file}.")
    self.follow_debug_setting()

  def follow_debug_setting(self):
    """Make the debug capture follow the `debug` setting: on while its stop time is ahead, appending to the newest
    file, and off once it is cleared or has passed, which clears it. Run every minute, it also pushes the growth of
    the file being written."""
    with self._debug_lock:
      stop_at = Settings().get("debug", "stop_at")
      if stop_at is not None and datetime.fromisoformat(stop_at) <= utcnow():
        Settings().set("debug", {"stop_at": None})
        stop_at = None
      if stop_at is not None and self.debug_file is None:
        files = self.debug_files()
        self._open_debug_file(files[0]["name"] if files else _new_debug_file())
      elif stop_at is None and self.debug_file is not None:
        logger.info(f"Debug capture stopped: {self.debug_file}.")
        self.debug_file = None
        self._queue.put(("close", None))
    self._debug_capture.check()

  def _open_debug_file(self, name: str):
    # Listed from the start, before the drain thread opens it.
    (self.debug_dir / name).touch()
    self._queue.put(("open", name))
    self.debug_file = name
    logger.info(f"Debug capture on until {Settings().get('debug', 'stop_at')} UTC: {name}.")

  def _debug_file_full(self, name: str):
    """The drain thread closed the file at its size limit: the capture is over."""
    with self._debug_lock:
      if self.debug_file != name:
        return
      self.debug_file = None
      Settings().set("debug", {"stop_at": None})
    logger.warning(f"Debug capture stopped: {name} reached {config.LOG_SHARD_MAX_BYTES // 2**20} MiB.")
    self._debug_capture.check()

  def debug_capture(self) -> dict:
    return {"stop_at": Settings().get("debug", "stop_at"), "files": self.debug_files()}

  def debug_files(self) -> list[dict]:
    """The debug captures' files, newest first."""
    files = []
    for path in sorted(self.debug_dir.glob("sage-debug-*.log"), reverse=True):
      stat = path.stat()
      files.append({
          "name": path.name,
          "size": stat.st_size,
          "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(tzinfo=None),
          "active": path.name == self.debug_file,
      })
    return files

  def debug_file_path(self, name: str) -> Path:
    if not _DEBUG_FILE.fullmatch(name):
      raise ValueError(f"Not a debug capture file: {name!r}")
    return self.debug_dir / name

  def delete_debug_file(self, name: str):
    path = self.debug_file_path(name)
    if name == self.debug_file:
      raise ValueError("The debug capture is writing this file; stop it first.")
    path.unlink()
    self._debug_capture.check()

  def _check_sizes(self, name: str):
    """After a write: trim the file's logs, and all logs together, if they are over their limits. Looked at at most
    every LOG_SIZE_CHECK_SECONDS, by one thread at a time, and never an error for the write."""
    now = time.monotonic()
    with self.lock:
      shard_due = now - self._checked.get(name, float("-inf")) >= config.LOG_SIZE_CHECK_SECONDS
      total_due = now - self._total_checked >= config.LOG_SIZE_CHECK_SECONDS
      if shard_due:
        self._checked[name] = now
      if total_due:
        self._total_checked = now
    if not (shard_due or total_due) or not self._trim_lock.acquire(blocking=False):
      return
    try:
      if shard_due:
        self.trim_shard(name)
      if total_due:
        self.trim_total()
    except Exception:
      logger.warning(f"Could not trim the logs of '{name}'.", exc_info=True)
    finally:
      self._trim_lock.release()

  @staticmethod
  def _scalar(db, sql: str):
    """The first value of a query, with its cursor closed."""
    cursor = db.execute_sql(sql)
    try:
      return cursor.fetchone()[0]
    finally:
      cursor.close()

  def _pages(self, shard) -> tuple[int, int, int]:
    """The page size, the pages of the shard, and how many of them are free."""
    page_size, pages, free = (self._scalar(shard["db"], f"PRAGMA {name}")
                              for name in ("page_size", "page_count", "freelist_count"))
    return page_size, pages, free

  def _live_bytes(self, shard) -> int:
    """The data in a shard: its pages less the free ones."""
    page_size, pages, free = self._pages(shard)
    return (pages - free) * page_size

  def _file_bytes(self, shard) -> int:
    """The size the shard's file has, or will have once the write-ahead log is checkpointed."""
    page_size, pages, _ = self._pages(shard)
    return pages * page_size

  def _count(self, shard) -> int:
    return self._scalar(shard["db"], f"SELECT COUNT(*) FROM {shard['lines']._meta.table_name}")

  def _oldest_ts(self, shard) -> str | None:
    return self._scalar(shard["db"], f"SELECT MIN(ts) FROM {shard['lines']._meta.table_name}")

  @staticmethod
  def _delete_oldest(shard, rows: int):
    """Delete the `rows` oldest lines by timestamp, a batch to a transaction."""
    lines = shard["lines"]
    done = 0
    while done < rows:
      step = min(config.LOG_TRIM_BATCH_ROWS, rows - done)
      with shard["db"].atomic("IMMEDIATE"):
        oldest = lines.select(lines.id).order_by(lines.ts, lines.id).limit(step)
        lines.delete().where(lines.id.in_(oldest)).execute()
      done += step

  def _compact(self, name: str, shard):
    """Merge the full-text index so the space of deleted lines is free, and rewrite a file that is far bigger
    than a shard may be."""
    db = shard["db"]
    if shard["index"] is not None:
      shard["index"].optimize()
    if self._file_bytes(shard) > config.LOG_SHARD_MAX_BYTES * config.LOG_SHARD_VACUUM_RATIO:
      try:
        db.execute_sql("VACUUM")
        db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
      except Exception:
        logger.warning(f"Could not rewrite the logs shard '{name}' to give back its space.", exc_info=True)

  def trim_shard(self, name: str) -> int:
    """Keep a file's logs within LOG_SHARD_MAX_BYTES by deleting its oldest lines. Returns the lines deleted."""
    shard = self._store(name)
    limit = config.LOG_SHARD_MAX_BYTES
    live = self._live_bytes(shard)
    removed = 0
    if live > limit:
      rows = self._count(shard)
      keep = max(config.LOG_KEEP_NEWEST_ROWS, int(rows * limit * config.LOG_TRIM_TO_SHARE / live))
      removed = max(rows - keep, 0)
      self._delete_oldest(shard, removed)
    if removed or self._file_bytes(shard) > limit * config.LOG_SHARD_VACUUM_RATIO:
      self._compact(name, shard)
    if removed:
      logger.info(f"Trimmed {removed} of the oldest log lines of '{name}' to keep it within {limit // 2**20} MiB.")
    return removed

  def total_limit(self) -> int:
    return min(config.LOG_TOTAL_MAX_BYTES, int(shutil.disk_usage(self.db_path).total * config.LOG_TOTAL_MAX_DISK_SHARE))

  def trim_total(self) -> int:
    """Keep all the logs together within `total_limit` by deleting the oldest lines of any file first, a batch at a
    time. Returns the lines deleted."""
    shards = {path.stem: self._store(path.stem) for path in sorted((Path(self.db_path) / "logs").glob("*.db"))}
    sizes = {name: self._live_bytes(shard) for name, shard in shards.items()}
    limit = self.total_limit()
    if sum(sizes.values()) <= limit:
      return 0
    excess = sum(sizes.values()) - limit * config.LOG_TRIM_TO_SHARE
    rows = {name: self._count(shard) for name, shard in shards.items()}
    bytes_per_row = {name: sizes[name] / max(rows[name], 1) for name in shards}
    removed = dict.fromkeys(shards, 0)
    while excess > 0:
      oldest = {name: self._oldest_ts(shards[name]) for name in shards
                if rows[name] - removed[name] > config.LOG_KEEP_NEWEST_ROWS}
      if not oldest:
        break
      name = min(oldest, key=oldest.get)
      step = min(config.LOG_TRIM_BATCH_ROWS, rows[name] - removed[name] - config.LOG_KEEP_NEWEST_ROWS)
      self._delete_oldest(shards[name], step)
      removed[name] += step
      excess -= step * bytes_per_row[name]
    for name, count in removed.items():
      if count:
        self._compact(name, shards[name])
        logger.info(f"Trimmed {count} of the oldest log lines of '{name}' to keep all logs within {limit // 2**20} MiB.")
    return sum(removed.values())

  def _drop_shard(self, key: str):
    """Close the cached connection for a shard and delete its files (`.db` plus
    the `-wal`/`-shm` siblings). Removing the file is how space for a deleted
    container is reclaimed -- its pages are never reused, so a row delete or
    VACUUM would not."""
    with self.lock:
      entry = self._dbs["logs"].pop(key, None)
    if entry is not None:
      try:
        entry["db"].close()
      except Exception:
        logger.warning(f"Failed to close logs shard '{key}' before removal.", exc_info=True)
    db_file = f"{self.db_path}/logs/{key}.db"
    for path in (db_file, f"{db_file}-wal", f"{db_file}-shm"):
      Path(path).unlink(missing_ok=True)

  def cleanup(self, days: int = config.RETENTION_DAYS):
    cutoff = utcnow() - timedelta(days=days)

    # Drop the files of apps that no longer exist (logs keyed by app qualified_name) and of any other name; the task
    # logs and the worker stack's containers are always kept.
    live = _SYSTEM_LOG_CONTAINERS | {_TASKS} | {
        application.qualified_name
        for application in Application.select(Application, Project).join(Project)
    }
    logs_dir = Path(self.db_path) / "logs"
    orphans = {path.stem for path in logs_dir.glob("*.db")} - live
    for key in sorted(orphans):
      self._drop_shard(key)
      logger.info(f"Removed orphaned logs shard '{key}'.")

    # Delete rows past the retention window, then 'optimize' to merge the FTS
    # index (the delete trigger only tombstones tokens, so segments accumulate
    # without it). Freed pages are reused by later inserts, so shards plateau.
    existing = {path.stem for path in logs_dir.glob("*.db")} & live
    deleted = 0
    for name in sorted(existing):
      shard = self._store(name)
      deleted += shard["lines"].delete().where(shard["lines"].ts < cutoff.isoformat()).execute()
      if shard["index"] is not None:
        shard["index"].optimize()

    trimmed = sum(self.trim_shard(name) for name in sorted(existing)) + self.trim_total()

    old_files = [file["name"] for file in self.debug_files() if not file["active"] and file["modified_at"] < cutoff]
    for name in old_files:
      (self.debug_dir / name).unlink(missing_ok=True)
    self._debug_capture.check()

    logger.info(
        f"Logs cleanup: removed {deleted} log rows older than {days} days and trimmed {trimmed} over the size limits; "
        f"dropped {len(orphans)} orphaned shard(s) and {len(old_files)} debug file(s).")


class _CaptureHandler(logging.Handler):
  """Hands every line to the drain thread while a debug capture is on, and the lines of a queued task, so the calling
  thread never blocks on a write."""

  def __init__(self, q: queue.SimpleQueue, debugging: Callable[[], bool]):
    super().__init__()
    self._queue = q
    self._debugging = debugging
    self.setFormatter(logging.Formatter("%(message)s"))
    self._debug_formatter = logging.Formatter(LOG_FORMAT)
    self.addFilter(ContextVarFilter())
    self.addFilter(SuppressTracebackFilter())

  def emit(self, record):
    try:
      if self._debugging():
        self._queue.put(("line", self._debug_formatter.format(record) + "\n"))
      lines = task_lines.get()
      if lines is not None:
        lines.write(partial(self._queue.put, ("task", {
            "task_id": task_id.get(),
            "sub_task_id": sub_task_id.get(),
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        })))
    except Exception:
      self.handleError(record)
