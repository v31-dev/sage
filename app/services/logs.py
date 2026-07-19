import logging
import os
import queue
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from peewee import CharField, Model, SqliteDatabase, TextField
from playhouse.sqlite_ext import FTS5Model, RowIDField, SearchField

from services.base import Base
from services.db import Application, Project
from utils.common import get_env
from utils.logging import LOG_FORMAT, ContextVarFilter, SuppressTracebackFilter

logger = logging.getLogger(__name__)

_SYSTEM_LOG_CONTAINERS = {"sage", "cloudflared", "traefik", "glances", "vector"}


class Logs(Base):
  """Per-container log shards (one SQLite DB each, with an FTS5 message index),
  plus in-process capture of the manager's own logs: a handler on the root logger
  hands formatted records to a drain thread that writes them to the `sage` shard."""

  def __init__(self):
    super().__init__()

    self.db_path = "/app/data"
    os.makedirs(f"{self.db_path}/logs", exist_ok=True)
    self._dbs = {"logs": {}}

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
    with shard["db"].atomic():
      ContainerLogs.insert_many(entries).execute()
      # FTS index updated automatically via containerlogs_ai trigger

    logger.info(f"Wrote {len(entries)} log entries for container {container}.")

  def append_self_log(self, entry: dict):
    """Insert one of the manager's own log records into its `sage` shard. Quiet
    by design (no logging) -- it runs from the capture drain thread, so a log
    here would recurse."""
    shard = self.get_logs_db("sage")
    shard["models"]["ContainerLogs"].insert(**entry).execute()

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

  def cleanup(self, days: int = 7):
    cutoff = datetime.now() - timedelta(days=days)

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
    logger.info(
        f"Logs cleanup: removed {deleted} log rows older than {days} days; "
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
          "hostname": get_env("HOSTNAME"),
          "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
          "stream": "stderr" if record.levelno >= logging.WARNING else "stdout",
          "message": self.format(record),
      })
    except Exception:
      self.handleError(record)
