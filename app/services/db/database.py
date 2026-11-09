import logging
import re
import sqlite3
from contextlib import closing

from peewee import IntegrityError, sort_models
from playhouse.migrate import SqliteMigrator, migrate

import config
from services.base import Base

from .models import (Application, Backup, Container, ContainerEvent, Domain, Instance, Notification, Project, Setting, Task, Volume, Worker,
                     WorkerEvent, db)

logger = logging.getLogger(__name__)

MODELS = [Setting, Instance, Project, Application, Volume, Domain, Worker, Container, ContainerEvent, WorkerEvent, Notification,
          Backup, Task]


class SchemaRefused(Exception):
  """The database can't take this version's schema: `reason` says why, and the message links the running version's
  release, where the fix is."""

  def __init__(self, reason: str):
    super().__init__(
        f"Sage {config.VERSION} can't update the schema of {config.DB_PATH}: {reason} The database was left as it was. "
        f"How to fix it: {config.RELEASE_PAGE_URL.format(version=config.VERSION)}")


def _table_schema(execute, table: str) -> tuple[dict[str, str], list[str]]:
  """What SQLite can't change in place in `table`: each column's definition with its foreign key, and the table's
  CHECK clauses, sorted."""
  columns = {name: f"{type}{' NOT NULL' if notnull else ''}{' PRIMARY KEY' if pk else ''}"
             for _, name, type, notnull, _, pk, _ in execute(f'PRAGMA table_xinfo("{table}")').fetchall()}
  for _, _, parent, column, target, _, on_delete, _ in execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
    columns[column] += f" REFERENCES {parent} ({target}) ON DELETE {on_delete}"
  (sql,) = execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
  checks = []
  for match in re.finditer(r"\bCHECK\s*\(", sql, re.IGNORECASE):
    depth, quote = 0, None
    for end in range(match.end() - 1, len(sql)):
      if quote:
        quote = None if sql[end] == quote else quote
      elif sql[end] in "'\"":
        quote = sql[end]
      elif sql[end] in "()":
        depth += 1 if sql[end] == "(" else -1
        if depth == 0:
          break
    checks.append(sql[match.start():end + 1])
  return columns, sorted(checks)


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
    tables, drop the indexes no model declares, add missing columns and drop those
    no model has, refuse a column whose definition differs from its model's, and
    create the missing indexes (a create skips a name that exists, so a changed
    index comes under a new name). Idempotent and safe to call any time the
    on-disk schema may lag the models -- at startup, or after a restore swaps in
    an older database snapshot. Also gives the manager its identity if it has none.

    The step is SQLite's procedure for schema changes: foreign keys off, every
    change in one transaction, a foreign key check, commit. Adding a NOT NULL
    column rebuilds its table, and with foreign keys on, dropping the old table
    would delete or block the rows that reference it. A changed column, or data
    that refuses the schema (an index, or the foreign key check), rolls the whole
    step back and raises `SchemaRefused`, whose message links the running
    version's release, where the fix is.
    """
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
      # The pragma is ignored inside a transaction.
      if db.execute_sql("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("The schema step must not run inside a transaction: foreign keys can't be turned off.")
      with db.atomic():
        # Undeclared indexes go before the columns: rebuilding a table to drop a column fails on an index that still
        # names it. New indexes go last: one on a column not added yet would be built on the string of its name (SQLite
        # reads a double-quoted name it doesn't know as a literal).
        for model in sort_models(MODELS):
          model._schema.create_table(safe=True)
        declared = {index._name for model in MODELS for index in model._meta.fields_to_index()}
        for model in MODELS:
          for index in db.get_indexes(model._meta.table_name):
            if index.name not in declared and not index.name.startswith("sqlite_autoindex_"):
              logger.info(f"Dropping index {index.name}, which no model declares.")
              db.execute_sql(f'DROP INDEX "{index.name}"')
        self._sync_columns()
        for model in MODELS:
          model._schema.create_indexes(safe=True)
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
      raise SchemaRefused(f"the data in it refuses the change ({e}).{refused_index}") from e
    finally:
      db.execute_sql("PRAGMA foreign_keys=ON")
    if not Instance.select().exists():
      Instance.create()

  def _sync_columns(self):
    """
    Add any model field that is missing from its table as a new column, drop any
    column that no model field has any more, and refuse the tables whose columns
    then differ from their models' in type, NOT NULL, primary key, foreign key or
    CHECK clauses: SQLite can't change those in place. The comparison is with the
    model's table built in a throwaway in-memory database.

    This is a dynamic, model-driven sync derived from the field definitions
    themselves, not hand-written per-column steps. Anything that cannot be
    applied (e.g. a not-null column without a default on a populated table)
    raises and crashes startup, which is the intended outcome.
    """
    migrator = SqliteMigrator(db)
    changed = []
    with closing(sqlite3.connect(":memory:")) as expected:
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
          # A rebuild of the table: SQLite's own DROP COLUMN refuses a column in a foreign key or an index.
          migrate(migrator.drop_column(table, column, legacy=True))
        expected.execute(*model._schema._create_table(safe=False).query())
        (columns, checks), (model_columns, model_checks) = (
            _table_schema(db.execute_sql, table), _table_schema(expected.execute, table))
        changed += [f"{table}.{name} is {columns[name]}, this version's is {definition}"
                    for name, definition in model_columns.items() if columns[name] != definition]
        if checks != model_checks:
          changed.append(f"{table} has the checks {'; '.join(checks) or 'none'}, "
                         f"this version's are {'; '.join(model_checks) or 'none'}")
    if changed:
      raise SchemaRefused(f"its tables differ from this version's where SQLite can't change them: {'; '.join(changed)}.")
