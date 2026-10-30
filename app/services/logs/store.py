"""The logs directory, `logs/` under the data directory: a SQLite file for each kind of log (see `Logs.FILES`) and
for each application, each opened once and kept within its size, all of them within theirs, and none of their lines
older than the retention."""
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from peewee import SQL, Model, NodeList, SqliteDatabase
from playhouse.sqlite_ext import FTS5Model

import config
from utils.common import utcnow
from utils.db import like_literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Store:
  """A file of the logs directory: its database, its lines, and their full-text index if they have one."""
  db: SqliteDatabase
  lines: type[Model]
  index: type[FTS5Model] | None = None

  def _scalar(self, sql: str):
    """The first value of a query, with its cursor closed."""
    cursor = self.db.execute_sql(sql)
    try:
      return cursor.fetchone()[0]
    finally:
      cursor.close()

  def pages(self) -> tuple[int, int, int]:
    """The page size, the pages of the file, and how many of them are free."""
    page_size, pages, free = (self._scalar(f"PRAGMA {name}") for name in ("page_size", "page_count", "freelist_count"))
    return page_size, pages, free

  def live_bytes(self) -> int:
    """The data in the file: its pages less the free ones."""
    page_size, pages, free = self.pages()
    return (pages - free) * page_size

  def file_bytes(self) -> int:
    """The size the file has, or will have once the write-ahead log is checkpointed."""
    page_size, pages, _ = self.pages()
    return pages * page_size

  def count(self) -> int:
    return self._scalar(f"SELECT COUNT(*) FROM {self.lines._meta.table_name}")

  def oldest_ts(self) -> str | None:
    return self._scalar(f"SELECT MIN(ts) FROM {self.lines._meta.table_name}")

  def delete_oldest(self, rows: int):
    """Delete the `rows` oldest lines by timestamp, a batch to a transaction."""
    lines = self.lines
    done = 0
    while done < rows:
      step = min(config.LOG_TRIM_BATCH_ROWS, rows - done)
      with self.db.atomic("IMMEDIATE"):
        oldest = lines.select(lines.id).order_by(lines.ts, lines.id).limit(step)
        lines.delete().where(lines.id.in_(oldest)).execute()
      done += step


def stored_time(value: datetime | str) -> str:
  """A time as the logs keep it, so that times compare as text: UTC with nine digits of fraction and `Z`. A datetime
  without a zone is UTC; text is an RFC 3339 time in UTC, as Vector and Traefik write it (they leave out trailing
  zeros), and is kept as it is when it is not one."""
  if isinstance(value, datetime):
    if value.tzinfo is not None:
      value = value.astimezone(timezone.utc)
    return f"{value:%Y-%m-%dT%H:%M:%S.%f}000Z"
  if not (match := re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", value)):
    return value
  return f"{match[1]}.{(match[2] or '').ljust(9, '0')}Z"


def open_db(path: Path) -> SqliteDatabase:
  db = SqliteDatabase(
      path,
      pragmas={
          "journal_mode": "wal",
          "cache_size": -2048,  # 2 MB, room for the full-text index and the B-trees
          "busy_timeout": 5000,  # wait up to 5 s before SQLITE_BUSY
          "synchronous": 1,  # NORMAL: a commit is not synced to disk, which logs can afford
      },
  )
  db.connect(reuse_if_open=True)
  return db


def newest(query, order) -> tuple[list[dict], bool]:
  """The newest LOG_QUERY_MAX_ROWS rows of a query, newest first by `order`, and whether there were more."""
  rows = list(query.order_by(order).limit(config.LOG_QUERY_MAX_ROWS + 1).dicts())
  return rows[:config.LOG_QUERY_MAX_ROWS], len(rows) > config.LOG_QUERY_MAX_ROWS


def contains(field, text: str):
  """`field` contains `text`, in any case."""
  return NodeList((field, SQL("LIKE ? ESCAPE '\\'", (f"%{like_literal(text)}%",))))


class StoreMixin:
  def __init__(self):
    super().__init__()
    self.db_path = config.DATA_DIR
    os.makedirs(f"{self.db_path}/logs", exist_ok=True)
    self._stores: dict[str, Store] = {}
    # When the size of each file, and of all together, was last looked at (monotonic seconds).
    self._checked: dict[str, float] = {}
    self._total_checked = float("-inf")
    self._trim_lock = threading.Lock()

  def _store(self, name: str) -> Store:
    """The file `name` of the logs directory, opened once, with its schema."""
    with self.lock:
      if name not in self._stores:
        self._stores[name] = self._schema(name)(open_db(Path(self.db_path) / "logs" / f"{name}.db"))
    return self._stores[name]

  def _insert(self, name: str, rows: list) -> list[dict]:
    """The rows, written to the file `name` in one transaction, with their ids. The one writer takes the next ids in
    order, so the ids in order are the rows'."""
    store = self._store(name)
    lines = store.lines
    with store.db.atomic("IMMEDIATE"):
      ids = sorted(row_id for (row_id,) in lines.insert_many(rows).returning(lines.id).tuples().execute())
    self._check_sizes(name)
    return [{"id": row_id, **row} for row_id, row in zip(ids, rows)]

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

  def _compact(self, name: str, store: Store):
    """Merge the full-text index so the space of deleted lines is free, and rewrite a file that is far bigger
    than a shard may be."""
    if store.index is not None:
      store.index.optimize()
    if store.file_bytes() > config.LOG_SHARD_MAX_BYTES * config.LOG_SHARD_VACUUM_RATIO:
      try:
        store.db.execute_sql("VACUUM")
        store.db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
      except Exception:
        logger.warning(f"Could not rewrite the logs shard '{name}' to give back its space.", exc_info=True)

  def trim_shard(self, name: str) -> int:
    """Keep a file's logs within LOG_SHARD_MAX_BYTES by deleting its oldest lines. Returns the lines deleted."""
    store = self._store(name)
    limit = config.LOG_SHARD_MAX_BYTES
    live = store.live_bytes()
    removed = 0
    if live > limit:
      rows = store.count()
      keep = max(config.LOG_KEEP_NEWEST_ROWS, int(rows * limit * config.LOG_TRIM_TO_SHARE / live))
      removed = max(rows - keep, 0)
      store.delete_oldest(removed)
    if removed or store.file_bytes() > limit * config.LOG_SHARD_VACUUM_RATIO:
      self._compact(name, store)
    if removed:
      logger.info(f"Trimmed {removed} of the oldest log lines of '{name}' to keep it within {limit // 2**20} MiB.")
    return removed

  def total_limit(self) -> int:
    return min(config.LOG_TOTAL_MAX_BYTES, int(shutil.disk_usage(self.db_path).total * config.LOG_TOTAL_MAX_DISK_SHARE))

  def trim_total(self) -> int:
    """Keep all the logs together within `total_limit` by deleting the oldest lines of any file first, a batch at a
    time. Returns the lines deleted."""
    stores = {path.stem: self._store(path.stem) for path in sorted((Path(self.db_path) / "logs").glob("*.db"))}
    sizes = {name: store.live_bytes() for name, store in stores.items()}
    limit = self.total_limit()
    if sum(sizes.values()) <= limit:
      return 0
    excess = sum(sizes.values()) - limit * config.LOG_TRIM_TO_SHARE
    rows = {name: store.count() for name, store in stores.items()}
    bytes_per_row = {name: sizes[name] / max(rows[name], 1) for name in stores}
    removed = dict.fromkeys(stores, 0)
    while excess > 0:
      oldest = {name: stores[name].oldest_ts() for name in stores
                if rows[name] - removed[name] > config.LOG_KEEP_NEWEST_ROWS}
      if not oldest:
        break
      name = min(oldest, key=oldest.get)
      step = min(config.LOG_TRIM_BATCH_ROWS, rows[name] - removed[name] - config.LOG_KEEP_NEWEST_ROWS)
      stores[name].delete_oldest(step)
      removed[name] += step
      excess -= step * bytes_per_row[name]
    for name, count in removed.items():
      if count:
        self._compact(name, stores[name])
        logger.info(f"Trimmed {count} of the oldest log lines of '{name}' to keep all logs within {limit // 2**20} MiB.")
    return sum(removed.values())

  def _drop_shard(self, name: str):
    """Close the file's connection and delete it, with its `-wal` and `-shm`: a deleted application's space comes back
    only this way, as its pages are never reused."""
    with self.lock:
      store = self._stores.pop(name, None)
    if store is not None:
      try:
        store.db.close()
      except Exception:
        logger.warning(f"Failed to close logs shard '{name}' before removal.", exc_info=True)
    db_file = f"{self.db_path}/logs/{name}.db"
    for path in (db_file, f"{db_file}-wal", f"{db_file}-shm"):
      Path(path).unlink(missing_ok=True)

  def cleanup(self, days: int = config.RETENTION_DAYS):
    """Drop the files that are neither a kind of log nor an application's shard, delete the lines older than `days`,
    and trim what is over the size limits."""
    cutoff = utcnow() - timedelta(days=days)

    live = set(self.FILES) | self._application_names()
    logs_dir = Path(self.db_path) / "logs"
    orphans = {path.stem for path in logs_dir.glob("*.db")} - live
    for name in sorted(orphans):
      self._drop_shard(name)
      logger.info(f"Removed orphaned logs shard '{name}'.")

    # A delete only marks the index's entries, so it is merged after; later writes reuse the freed pages.
    existing = {path.stem for path in logs_dir.glob("*.db")} & live
    deleted = 0
    for name in sorted(existing):
      store = self._store(name)
      deleted += store.lines.delete().where(store.lines.ts < stored_time(cutoff)).execute()
      if store.index is not None:
        store.index.optimize()

    trimmed = sum(self.trim_shard(name) for name in sorted(existing)) + self.trim_total()

    logger.info(
        f"Logs cleanup: removed {deleted} log rows older than {days} days and trimmed {trimmed} over the size limits; "
        f"dropped {len(orphans)} orphaned shard(s).")
