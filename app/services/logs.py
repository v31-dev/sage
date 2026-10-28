import logging
import os
import queue
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from peewee import CharField, Model, SqliteDatabase, TextField
from playhouse.sqlite_ext import FTS5Model, RowIDField, SearchField

import config
from services.base import Base
from services.db import Application, Project
from utils.common import get_env, utcnow
from utils.logging import LOG_FORMAT, ContextVarFilter, SuppressTracebackFilter

logger = logging.getLogger(__name__)

_SYSTEM_LOG_CONTAINERS = {"sage", "cloudflared", "traefik", "telegraf", "vector"}


class Logs(Base):
  """Per-container log shards (one SQLite DB each, with an FTS5 message index),
  plus in-process capture of the manager's own logs: a handler on the root logger
  hands formatted records to a drain thread that writes them to the `sage` shard."""

  def __init__(self):
    super().__init__()

    self.db_path = config.DATA_DIR
    os.makedirs(f"{self.db_path}/logs", exist_ok=True)
    self._dbs = {"logs": {}}
    # When the size of each shard, and of all together, was last looked at (monotonic seconds).
    self._checked: dict[str, float] = {}
    self._total_checked = float("-inf")
    self._trim_lock = threading.Lock()

    # The handler enqueues formatted records; the drain thread writes them to the
    # `sage` shard off the calling thread (often the event loop).
    self._queue = queue.SimpleQueue()
    threading.Thread(target=self._capture_loop, name="sage-log-capture", daemon=True).start()

    handler = _CaptureHandler(self._queue)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(ContextVarFilter())
    handler.addFilter(SuppressTracebackFilter())
    logging.getLogger().addHandler(handler)

  def _capture_loop(self):
    while True:
      entry = self._queue.get()
      try:
        self.append_self_log(entry)
      except Exception:
        pass  # never let a write error kill the drain thread or feed back

  def get_logs_db(self, container):
    """
    1 database per container.
    ContainerLogs stores all fields; ContainerLogsIndex is an FTS5 virtual table
    that indexes only the message text for fast full-text search.
    """
    # container is a qualified application name; reject anything that could escape
    # the logs directory when interpolated into the shard file path.
    if not re.fullmatch(r"[a-z0-9-]+", container or ""):
      raise ValueError(f"Invalid container name: {container!r}")

    with self.lock:
      if container not in self._dbs["logs"]:
        path = f"{self.db_path}/logs/{container}.db"
        db = SqliteDatabase(
            path,
            pragmas={
                "journal_mode": "wal",
                "cache_size": -2048,  # 2MB — FTS index thrashing prevented; room for B-tree
                "busy_timeout": 5000,  # Wait up to 5s before returning SQLITE_BUSY.
                "synchronous": 1,  # NORMAL — commit writes faster, acceptable for logs
            },
        )

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

        db.connect(reuse_if_open=True)
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
        self._dbs["logs"][container] = {
            "db": db,
            "models": {
                "ContainerLogs": ContainerLogs,
                "ContainerLogsIndex": ContainerLogsIndex,
            },
        }

    return self._dbs["logs"][container]

  def write_logs(self, container: str, entries: list):
    shard = self.get_logs_db(container)
    ContainerLogs = shard["models"]["ContainerLogs"]
    with shard["db"].atomic("IMMEDIATE"):
      ContainerLogs.insert_many(entries).execute()
      # FTS index updated automatically via containerlogs_ai trigger

    logger.info(f"Wrote {len(entries)} log entries for container {container}.")
    self._check_sizes(container)

  def append_self_log(self, entry: dict):
    """Insert one of the manager's own log records into its `sage` shard. Quiet
    by design (no logging) -- it runs from the capture drain thread, so a log
    here would recurse."""
    shard = self.get_logs_db("sage")
    shard["models"]["ContainerLogs"].insert(**entry).execute()
    self._check_sizes("sage")

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
    ContainerLogs = shard["models"]["ContainerLogs"]
    ContainerLogsIndex = shard["models"]["ContainerLogsIndex"]

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

  def _check_sizes(self, container: str):
    """After a write: trim the container's logs, and all logs together, if they are over their limits. Looked at
    at most every LOG_SIZE_CHECK_SECONDS, by one thread at a time, and never an error for the write."""
    now = time.monotonic()
    with self.lock:
      shard_due = now - self._checked.get(container, float("-inf")) >= config.LOG_SIZE_CHECK_SECONDS
      total_due = now - self._total_checked >= config.LOG_SIZE_CHECK_SECONDS
      if shard_due:
        self._checked[container] = now
      if total_due:
        self._total_checked = now
    if not (shard_due or total_due) or not self._trim_lock.acquire(blocking=False):
      return
    try:
      if shard_due:
        self.trim_shard(container)
      if total_due:
        self.trim_total()
    except Exception:
      logger.warning(f"Could not trim the logs of '{container}'.", exc_info=True)
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
    """The data in a shard: its pages less the free ones, which later writes reuse."""
    page_size, pages, free = self._pages(shard)
    return (pages - free) * page_size

  def _file_bytes(self, shard) -> int:
    """The size the shard's file has, or will have once the write-ahead log is checkpointed."""
    page_size, pages, _ = self._pages(shard)
    return pages * page_size

  def _count(self, shard) -> int:
    return self._scalar(shard["db"], "SELECT COUNT(*) FROM containerlogs")

  def _oldest_ts(self, shard) -> str | None:
    return self._scalar(shard["db"], "SELECT MIN(ts) FROM containerlogs")

  @staticmethod
  def _delete_oldest(shard, rows: int):
    """Delete the `rows` oldest lines by timestamp, a batch to a transaction so a writer never waits long."""
    ContainerLogs = shard["models"]["ContainerLogs"]
    done = 0
    while done < rows:
      step = min(config.LOG_TRIM_BATCH_ROWS, rows - done)
      with shard["db"].atomic("IMMEDIATE"):
        oldest = ContainerLogs.select(ContainerLogs.id).order_by(ContainerLogs.ts, ContainerLogs.id).limit(step)
        ContainerLogs.delete().where(ContainerLogs.id.in_(oldest)).execute()
      done += step

  def _compact(self, container: str, shard):
    """Merge the full-text index so the space of deleted lines is free, and rewrite a file that is far bigger
    than a shard may be."""
    db = shard["db"]
    db.execute_sql("INSERT INTO containerlogsindex(containerlogsindex) VALUES('optimize')")
    if self._file_bytes(shard) > config.LOG_SHARD_MAX_BYTES * config.LOG_SHARD_VACUUM_RATIO:
      try:
        db.execute_sql("VACUUM")
        db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
      except Exception:
        logger.warning(f"Could not rewrite the logs shard '{container}' to give back its space.", exc_info=True)

  def trim_shard(self, container: str) -> int:
    """Keep a container's logs within LOG_SHARD_MAX_BYTES by deleting its oldest lines. Returns the lines deleted."""
    shard = self.get_logs_db(container)
    limit = config.LOG_SHARD_MAX_BYTES
    live = self._live_bytes(shard)
    removed = 0
    if live > limit:
      rows = self._count(shard)
      keep = max(config.LOG_KEEP_NEWEST_ROWS, int(rows * limit * config.LOG_TRIM_TO_SHARE / live))
      removed = max(rows - keep, 0)
      self._delete_oldest(shard, removed)
    if removed or self._file_bytes(shard) > limit * config.LOG_SHARD_VACUUM_RATIO:
      self._compact(container, shard)
    if removed:
      logger.info(f"Trimmed {removed} of the oldest log lines of '{container}' to keep it within {limit // 2**20} MiB.")
    return removed

  def total_limit(self) -> int:
    return min(config.LOG_TOTAL_MAX_BYTES, int(shutil.disk_usage(self.db_path).total * config.LOG_TOTAL_MAX_DISK_SHARE))

  def trim_total(self) -> int:
    """Keep all the logs together within `total_limit` by deleting the oldest lines of any container first, a
    batch at a time. Returns the lines deleted."""
    shards = {path.stem: self.get_logs_db(path.stem) for path in sorted((Path(self.db_path) / "logs").glob("*.db"))}
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

    # Drop shards for apps that no longer exist (logs keyed by app qualified_name);
    # system containers (manager + worker edge stack) are always kept.
    live_containers = _SYSTEM_LOG_CONTAINERS | {
        application.qualified_name
        for application in Application.select(Application, Project).join(Project)
    }
    logs_dir = Path(self.db_path) / "logs"
    orphans = {path.stem for path in logs_dir.glob("*.db")} - live_containers
    for key in sorted(orphans):
      self._drop_shard(key)
      logger.info(f"Removed orphaned logs shard '{key}'.")

    # Delete rows past the retention window, then 'optimize' to merge the FTS
    # index (the delete trigger only tombstones tokens, so segments accumulate
    # without it). Freed pages are reused by later inserts, so shards plateau.
    existing = {path.stem for path in logs_dir.glob("*.db")} & live_containers
    deleted = 0
    for container in sorted(existing):
      db_info = self.get_logs_db(container)
      ContainerLogs = db_info["models"]["ContainerLogs"]
      deleted += ContainerLogs.delete().where(ContainerLogs.ts < cutoff.isoformat()).execute()
      db_info["db"].execute_sql("INSERT INTO containerlogsindex(containerlogsindex) VALUES('optimize')")

    trimmed = sum(self.trim_shard(container) for container in sorted(existing)) + self.trim_total()
    logger.info(
        f"Logs cleanup: removed {deleted} log rows older than {days} days and trimmed {trimmed} over the size limits; "
        f"dropped {len(orphans)} orphaned shard(s).")


class _CaptureHandler(logging.Handler):
  """Formats the manager's own log records and enqueues them for the drain
  thread. Kept off the SQLite write so the calling thread never blocks on it."""

  def __init__(self, q: queue.SimpleQueue):
    super().__init__()
    self._queue = q

  def emit(self, record):
    try:
      self._queue.put({
          "hostname": get_env("TS_NAME"),
          "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
          "stream": "stderr" if record.levelno >= logging.WARNING else "stdout",
          "message": self.format(record),
      })
    except Exception:
      self.handleError(record)
