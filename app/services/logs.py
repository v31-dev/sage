import json
import logging
import os
import queue
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from peewee import SQL, CharField, FloatField, IntegerField, Model, NodeList, SqliteDatabase, TextField, fn
from playhouse.sqlite_ext import FTS5Model, RowIDField, SearchField

import config
from services.base import Base
from services.db import Application, Project
from services.updates import UpdateFeed, Updates
from utils.common import utcnow
from utils.db import like_literal
from utils.logging import ContextVarFilter, SuppressTracebackFilter, sub_task_id, task_id, task_lines

logger = logging.getLogger(__name__)

# The files beside the applications' shards: the task logs and Traefik's requests. An application's shard is named
# `project-app`, so neither is one.
_TASKS = "tasks"
_REQUESTS = "requests"
# The container whose access lines are the requests.
_TRAEFIK = "traefik"


class Logs(Base):
  """Per-application log shards (one SQLite DB each, with an FTS5 message index), the requests (Traefik's access
  lines, in columns) and the task logs (the lines the manager logs inside its queued tasks). A handler on the root
  logger hands a task's lines to a drain thread to write."""

  def __init__(self):
    super().__init__()

    self.db_path = config.DATA_DIR
    os.makedirs(f"{self.db_path}/logs", exist_ok=True)
    self._dbs = {"logs": {}}
    # When the size of each file, and of all together, was last looked at (monotonic seconds).
    self._checked: dict[str, float] = {}
    self._total_checked = float("-inf")
    self._trim_lock = threading.Lock()

    # The handler enqueues the lines; the drain thread writes them off the calling thread (often the event loop).
    self._queue = queue.SimpleQueue()

  def start_capture(self):
    """Write the lines of queued tasks from now on: the handler on the root logger, and the drain thread."""
    threading.Thread(target=self._capture_loop, name="sage-log-capture", daemon=True).start()
    logging.getLogger().addHandler(_CaptureHandler(self._queue))

  def _capture_loop(self):
    while True:
      entry = self._queue.get()
      try:
        self.write_task_line(entry)
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
    if not re.fullmatch(r"[a-z0-9-]+", container or "") or container in (_TASKS, _REQUESTS):
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
            options = {"content": ContainerLogs, "content_rowid": "id", "tokenize": "trigram"}

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

  def requests_db(self):
    with self.lock:
      if _REQUESTS not in self._dbs["logs"]:
        db = self._open(_REQUESTS)

        class Requests(Model):
          ts = CharField()
          worker = CharField()
          host = CharField()
          method = CharField()
          path = TextField()
          status = IntegerField()
          duration_ms = FloatField()
          router = CharField()
          service = CharField()
          client = CharField()
          size = IntegerField()

          class Meta:
            database = db
            indexes = ((("ts",), False),)

        db.create_tables([Requests], safe=True)
        self._dbs["logs"][_REQUESTS] = {"db": db, "lines": Requests, "index": None}

    return self._dbs["logs"][_REQUESTS]

  def _store(self, name: str):
    """The task logs, the requests or an application's shard, by its file's name."""
    if name == _TASKS:
      return self.task_logs_db()
    if name == _REQUESTS:
      return self.requests_db()
    return self.get_logs_db(name)

  def ingest(self, hostname: str, body: bytes) -> int:
    """A push from a worker's Vector, one event a line: Traefik's access lines to the requests, and each application's
    lines to its shard, one write per file. Traefik's own lines are dropped. Returns how many lines were skipped (not
    an event, or of a container whose name cannot be a file); raises after the other writes when one fails."""
    by_container: dict[str, list] = {}
    requests = []
    skipped = 0
    for line in body.splitlines():
      if not line.strip():
        continue
      try:
        event = json.loads(line)
        container, ts, message = event["container_name"], event["timestamp"], event["message"]
      except (ValueError, KeyError, TypeError):
        skipped += 1
        continue
      if container == _TRAEFIK:
        if (request := _request(hostname, message)) is not None:
          requests.append(request)
      else:
        by_container.setdefault(container, []).append(
            {"hostname": hostname, "ts": ts, "stream": event.get("stream", ""), "message": message})

    failed = None
    writes = [(_REQUESTS, requests, self.write_requests)] if requests else []
    writes += [(container, rows, partial(self.write_logs, container)) for container, rows in by_container.items()]
    for name, rows, write in writes:
      try:
        write(rows)
      except ValueError:
        skipped += len(rows)
      except Exception as e:
        logger.warning(f"Could not write {len(rows)} log lines of '{name}': {e}")
        failed = failed or e
    if failed is not None:
      raise failed
    return skipped

  def _insert(self, name: str, store: dict, rows: list) -> list[dict]:
    """The rows, written in one transaction, with their ids. The one writer takes the next ids in order, so the ids
    in order are the rows'."""
    lines = store["lines"]
    with store["db"].atomic("IMMEDIATE"):
      ids = sorted(row_id for (row_id,) in lines.insert_many(rows).returning(lines.id).tuples().execute())
    self._check_sizes(name)
    return [{"id": row_id, **row} for row_id, row in zip(ids, rows)]

  def write_logs(self, container: str, entries: list):
    """An application's lines, to its shard and its feed."""
    stored = self._insert(container, self.get_logs_db(container), entries)
    Updates().publish(UpdateFeed.APPLICATION_LOGS, stored, key=container)

  def write_requests(self, rows: list):
    stored = self._insert(_REQUESTS, self.requests_db(), rows)
    Updates().publish(UpdateFeed.REQUESTS, stored)

  def write_task_line(self, entry: dict):
    """A task's line, to the task logs and to the feeds of the task and of its part."""
    row = {"id": self.task_logs_db()["lines"].insert(**entry).execute(), **entry}
    self._check_sizes(_TASKS)
    task, part = entry["task_id"], entry["sub_task_id"]
    Updates().publish(UpdateFeed.TASK_LOGS, [row], key=task)
    if part:
      Updates().publish(UpdateFeed.TASK_LOGS, [row], key=f"{task}/{part}")

  def query_logs(self, container: str, hostname: str = "", search: str = "", from_ts: str = "", to_ts: str = "",
                 after_id: int = 0) -> dict:
    """An application's lines, in the order they were stored: the newest LOG_QUERY_MAX_ROWS (after the line
    `after_id`, if given) from `hostname` (all workers if empty) that contain `search` anywhere, in any case, and whose
    time is at or after `from_ts` and before `to_ts`, and whether there were more. The search walks the message index newest first (the trigram tokenizer
    finds three characters or more through it; shorter text, or a shard indexed before it, is scanned) and stops at
    the cap."""
    shard = self.get_logs_db(container)
    lines, index = shard["lines"], shard["index"]
    query = lines.select()
    order = lines.id.desc()
    if search:
      query = query.join(index, on=(lines.id == index.rowid)).where(_contains(index.message, search))
      order = index.rowid.desc()
    if hostname:
      query = query.where(lines.hostname == hostname)
    if from_ts:
      query = query.where(lines.ts >= from_ts)
    if to_ts:
      query = query.where(lines.ts < to_ts)
    if after_id:
      query = query.where(lines.id > after_id)
    rows = list(query.order_by(order).limit(config.LOG_QUERY_MAX_ROWS + 1).dicts())
    return {"lines": rows[:config.LOG_QUERY_MAX_ROWS][::-1], "more": len(rows) > config.LOG_QUERY_MAX_ROWS}

  def query_requests(self, worker: str = "", host: str = "", method: str = "", status_class: int | None = None,
                     path: str = "", min_duration_ms: float | None = None, from_ts: str = "", to_ts: str = "",
                     after_id: int = 0) -> dict:
    """The newest LOG_QUERY_MAX_ROWS requests (after the request `after_id`, if given), newest first, that match: `worker` and `method` exactly, `host` and
    `path` anywhere in any case, `status_class` the first digit of the status (4 for 4xx), `min_duration_ms` or
    slower, and a start at or after `from_ts` and before `to_ts`; and whether there were more."""
    requests = self.requests_db()["lines"]
    query = requests.select()
    if worker:
      query = query.where(requests.worker == worker)
    if host:
      query = query.where(_contains(requests.host, host))
    if method:
      query = query.where(requests.method == method)
    if status_class is not None:
      query = query.where((requests.status >= status_class * 100) & (requests.status < (status_class + 1) * 100))
    if path:
      query = query.where(_contains(requests.path, path))
    if min_duration_ms is not None:
      query = query.where(requests.duration_ms >= min_duration_ms)
    if from_ts:
      query = query.where(requests.ts >= from_ts)
    if to_ts:
      query = query.where(requests.ts < to_ts)
    if after_id:
      query = query.where(requests.id > after_id)
    rows = list(query.order_by(requests.id.desc()).limit(config.LOG_QUERY_MAX_ROWS + 1).dicts())
    return {"requests": rows[:config.LOG_QUERY_MAX_ROWS], "more": len(rows) > config.LOG_QUERY_MAX_ROWS}

  def query_task_logs(self, task_id: str, sub_task_id: str = "", after_id: int = 0) -> dict:
    """A task's lines, or one container's part of it, in the order they were written: the newest LOG_QUERY_MAX_ROWS
    (after the line `after_id`, if given), and whether there were more."""
    TaskLogs = self.task_logs_db()["lines"]
    query = TaskLogs.select().where(TaskLogs.task_id == task_id)
    if sub_task_id:
      query = query.where(TaskLogs.sub_task_id == sub_task_id)
    if after_id:
      query = query.where(TaskLogs.id > after_id)
    rows = list(query.order_by(TaskLogs.id.desc()).limit(config.LOG_QUERY_MAX_ROWS + 1).dicts())
    return {"lines": rows[:config.LOG_QUERY_MAX_ROWS][::-1], "more": len(rows) > config.LOG_QUERY_MAX_ROWS}

  def query_task_parts(self, task_id: str) -> list[str]:
    """The ids of the parts of a task that logged lines, in the order of their first line."""
    TaskLogs = self.task_logs_db()["lines"]
    query = (TaskLogs.select(TaskLogs.sub_task_id)
             .where((TaskLogs.task_id == task_id) & (TaskLogs.sub_task_id != ""))
             .group_by(TaskLogs.sub_task_id).order_by(fn.MIN(TaskLogs.id)))
    return [part for (part,) in query.tuples()]

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
    # logs and the requests are always kept.
    live = {_TASKS, _REQUESTS} | {
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

    logger.info(
        f"Logs cleanup: removed {deleted} log rows older than {days} days and trimmed {trimmed} over the size limits; "
        f"dropped {len(orphans)} orphaned shard(s).")


class _CaptureHandler(logging.Handler):
  """Hands the lines of a queued task to the drain thread, so the calling thread never blocks on a write."""

  def __init__(self, q: queue.SimpleQueue):
    super().__init__()
    self._queue = q
    self.setFormatter(logging.Formatter("%(message)s"))
    self.addFilter(ContextVarFilter())
    self.addFilter(SuppressTracebackFilter())

  def emit(self, record):
    try:
      lines = task_lines.get()
      if lines is not None:
        lines.write(partial(self._queue.put, {
            "task_id": task_id.get(),
            "sub_task_id": sub_task_id.get(),
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }))
    except Exception:
      self.handleError(record)


def _request(hostname: str, message: str) -> dict | None:
  """Traefik's access line (JSON) as a request; None for one of its own lines."""
  try:
    line = json.loads(message)
  except ValueError:
    return None
  if not isinstance(line, dict) or "RequestMethod" not in line:
    return None
  return {
      "ts": timestamp(line.get("StartUTC", "")),
      "worker": hostname,
      "host": line.get("RequestHost", ""),
      "method": line["RequestMethod"],
      "path": line.get("RequestPath", ""),
      "status": int(line.get("DownstreamStatus") or 0),
      # A Go duration, in nanoseconds.
      "duration_ms": (line.get("Duration") or 0) / 1e6,
      "router": line.get("RouterName", ""),
      "service": line.get("ServiceName", ""),
      "client": line.get("ClientHost", ""),
      "size": int(line.get("DownstreamContentSize") or 0),
  }


def timestamp(text: str) -> str:
  """A UTC time as the request store keeps it, with nine digits of fraction, so that times compare as text: Traefik
  leaves out trailing zeros."""
  if not (match := re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", text)):
    return text
  return f"{match[1]}.{(match[2] or '').ljust(9, '0')}Z"


def _contains(field, text: str):
  """`field` contains `text`, in any case."""
  return NodeList((field, SQL("LIKE ? ESCAPE '\\'", (f"%{like_literal(text)}%",))))
