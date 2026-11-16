import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import config
from services.base import Base
from utils.common import new_id, utcnow

from . import migration_log, migrations, migrator
from .models import (Application, Backup, Container, ContainerEvent, Domain, Instance, Notification, Project, Setting, Task, Volume, Worker,
                     WorkerEvent, db)

logger = logging.getLogger(__name__)

MODELS = [Setting, Instance, Project, Application, Volume, Domain, Worker, Container, ContainerEvent, WorkerEvent, Notification,
          Backup, Task]
# What happened rather than what is configured: never in a platform backup, and emptied by a restore.
HISTORY = (ContainerEvent, WorkerEvent, Notification, Task)


class Database(Base):
  def __init__(self):
    super().__init__()

    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.init(
        str(config.DB_PATH),
        pragmas={
            "journal_mode": "wal",  # Allow readers while writer active.
            "cache_size": -64000,  # 64 MB page cache.
            "foreign_keys": 1,  # Enforce FK constraints.
            "busy_timeout": 5000,  # Wait up to 5s before returning SQLITE_BUSY.
        },
    )
    db.connect(reuse_if_open=True)

    self.migrate()

    logger.info("Connected to database.")

  def migrate(self, run: str = migration_log.BOOT, trigger: str = "boot"):
    """Brings the database to this version's schema with its migration steps (`migrator.migrate`), at boot and
    after a platform restore's install, as `run` of the migration history, and gives the manager its identity if it has
    none."""
    migrator.migrate(db, migrations.BASELINE, migrations.STEPS, migrator.models_schema(MODELS), run=run, trigger=trigger)
    if not Instance.select().exists():
      Instance.create()

  def snapshot(self, path: Path):
    """Copies the live database to `path` as a platform backup holds it: its configuration, without the history or
    the Backup rows (they mirror the store, whose discovery rebuilds them) and without the containers' intents (a
    restore stops every container until it is deployed again), compacted. Blocking: offload it."""
    with closing(sqlite3.connect(path)) as copy:
      db.connection().backup(copy)
      for model in (*HISTORY, Backup):
        copy.execute(f'DELETE FROM "{model._meta.table_name}"')
      copy.execute(f'UPDATE "{Container._meta.table_name}" SET intent = NULL')
      copy.commit()
      copy.execute("VACUUM")

  def install(self, path: Path, settings: dict[str, dict]):
    """Replaces the live database with the platform backup at `path`. The backup file is made the restored state
    first, in place: a fresh state generation, the sections in `settings` (key to values) kept over the backup's, and
    the history and the Backup rows emptied (discovery rebuilds those). Then a safety copy of the live database, the
    backup copied in, which SQLite commits whole or not at all, and migrated as at boot. A failure copies the safety copy back and raises; the safety copy is removed either way.
    Blocking: offload it, with nothing else writing meanwhile: the platform restore runs it alone, in the queue's
    restoring state, with changes and the workers' reports refused."""
    with closing(sqlite3.connect(path)) as backup:
      if (result := backup.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
        raise ValueError(f"The backup is not a sound database: {result}.")
      tables = {name for (name,) in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
      with backup:
        # A backup without the column gets a fresh one from the migration step that adds it.
        if "state_generation" in {name for _, name, *_ in backup.execute(f'PRAGMA table_info("{Instance._meta.table_name}")')}:
          backup.execute(f'UPDATE "{Instance._meta.table_name}" SET state_generation = ?', (new_id(),))
        now = str(utcnow())
        for key, values in settings.items():
          backup.execute(f'INSERT INTO "{Setting._meta.table_name}" (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) '
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, Setting.value.db_value(values), now, now))
        for model in (*HISTORY, Backup):
          if model._meta.table_name in tables:
            backup.execute(f'DELETE FROM "{model._meta.table_name}"')
    safety = config.DB_PATH.with_name(f"{config.DB_PATH.name}.restore-{utcnow():%Y%m%d_%H%M%S}")
    with closing(sqlite3.connect(safety)) as copy:
      db.connection().backup(copy)
    try:
      with closing(sqlite3.connect(path)) as backup:
        backup.backup(db.connection())
      self.migrate(new_id(), "restore")
    except Exception:
      with closing(sqlite3.connect(safety)) as copy:
        copy.backup(db.connection())
      raise
    finally:
      safety.unlink(missing_ok=True)

  def remove_safety_copies(self, days: int = config.RETENTION_DAYS):
    """Removes the safety copies older than `days` beside the live database, which only an install killed part way
    leaves."""
    before = utcnow() - timedelta(days=days)
    for path in config.DB_PATH.parent.glob(f"{config.DB_PATH.name}.restore-*"):
      if datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(tzinfo=None) < before:
        path.unlink(missing_ok=True)
        logger.info(f"Removed {path.name}, the safety copy of an interrupted restore.")
