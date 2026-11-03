import logging
import re

from peewee import IntegrityError
from playhouse.migrate import SqliteMigrator, migrate

import config
from services.base import Base

from .models import Application, Backup, Container, Domain, Event, Instance, Notification, Project, Setting, Task, Volume, Worker, db

logger = logging.getLogger(__name__)

MODELS = [Setting, Instance, Project, Application, Volume, Domain, Worker, Container, Event, Notification, Backup, Task]


class SchemaRefused(Exception):
  """The data in the database refuses this version's schema."""


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

    self.reconcile_schema()

    logger.info("Connected to database.")

  def reconcile_schema(self):
    """
    Bring the live database schema up to the current models: create missing
    tables and add missing columns. Idempotent and safe to call any time the
    on-disk schema may lag the models -- at startup, or after a restore swaps in
    an older database snapshot. Also gives the manager its identity if it has none.

    The step is SQLite's procedure for schema changes: foreign keys off, every
    change in one transaction, a foreign key check, commit. Adding a NOT NULL
    column rebuilds its table, and with foreign keys on, dropping the old table
    would delete or block the rows that reference it. Data that refuses the
    schema (an index, or the foreign key check) rolls the whole step back and
    raises `SchemaRefused`, whose message links the running version's release,
    where the fix is.
    """
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
      # The pragma is ignored inside a transaction.
      if db.execute_sql("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("The schema step must not run inside a transaction: foreign keys can't be turned off.")
      with db.atomic():
        db.create_tables(MODELS, safe=True)
        self._sync_columns()
        if broken := db.execute_sql("PRAGMA foreign_key_check").fetchall():
          raise IntegrityError("FOREIGN KEY check failed: " + ", ".join(
              f"{table} row {row} points at a missing {parent}" for table, row, parent, _ in broken))
    except IntegrityError as e:
      refused_index = ""
      if named := re.search(r"index '([^']+)'", str(e)):
        for model in MODELS:
          for index in model._meta.fields_to_index():
            sql = db.get_sql_context().sql(index).query()[0]
            if f'"{named[1]}"' in sql:
              refused_index = f" The refused index: {sql}."
      raise SchemaRefused(
          f"Sage {config.VERSION} can't update the schema of {config.DB_PATH}: the data in it refuses the change "
          f"({e}).{refused_index} The database was left as it was. "
          f"How to fix it: {config.RELEASE_PAGE_URL.format(version=config.VERSION)}") from e
    finally:
      db.execute_sql("PRAGMA foreign_keys=ON")
    if not Instance.select().exists():
      Instance.create()

  def _sync_columns(self):
    """
    Add any model field that is missing from its table as a new column, and drop any
    column that no model field has any more.

    This is a dynamic, model-driven sync derived from the field definitions
    themselves, not hand-written per-column steps. Anything that cannot be
    applied (e.g. a not-null column without a default on a populated table)
    raises and crashes startup, which is the intended outcome.
    """
    migrator = SqliteMigrator(db)
    for model in MODELS:
      table = model._meta.table_name
      existing = {column.name for column in db.get_columns(table)}
      wanted = {field.column_name for field in model._meta.sorted_fields}
      for field in model._meta.sorted_fields:
        if field.column_name in existing:
          continue
        logger.info(f"Adding missing column {table}.{field.column_name}.")
        migrate(migrator.add_column(table, field.column_name, field))
      for column in sorted(existing - wanted):
        logger.info(f"Dropping column {table}.{column}, which no model has.")
        migrate(migrator.drop_column(table, column))
