"""The runner every SQLite file of Sage's goes through. A file's version is its `PRAGMA user_version`, the number of the
last step applied; version 0 is the baseline, 0.1.3's schema. Step N takes version N-1's schema to N's, written against
that schema and never with the models, which describe only the latest one. A schema is given as its SQL. Each package
that owns files keeps their baselines and steps in its `migrations.py`."""
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from peewee import IntegrityError, Model, SqliteDatabase, sort_models

import config

from . import migration_log

logger = logging.getLogger(__name__)

BASELINE_RELEASE = "0.1.3"
# A side file's step that starts it empty at the latest version, for a change its rows can't follow cleanly.
RECREATE = "recreate"


class SchemaRefused(Exception):
  """The file can't take this version's schema: `reason` says why, and the message links the running version's
  release."""

  def __init__(self, path, reason: str):
    self.reason = reason
    super().__init__(
        f"Sage {config.VERSION} can't update the schema of {path}: {reason} The database was left as it was. "
        f"How to fix it: {config.RELEASE_PAGE_URL.format(version=config.VERSION)}")


def models_schema(models: list[type[Model]]) -> str:
  """The SQL that creates `models`' tables and indexes."""
  queries = [query.query() for model in sort_models(models)
             for query in (model._schema._create_table(safe=False), *model._schema._create_indexes(safe=False))]
  assert not any(params for _, params in queries)
  return "".join(f"{sql};\n" for sql, _ in queries)


def statements(script: str) -> list[str]:
  """The statements of an SQL script, each complete as SQLite reads it (a trigger's body holds semicolons)."""
  found, pending = [], ""
  for line in script.splitlines(keepends=True):
    pending += line
    if sqlite3.complete_statement(pending):
      found.append(pending.strip())
      pending = ""
  return found


def describe(execute) -> dict[str, dict]:
  """The schema a connection's `execute` sees, by table: each column's definition with its foreign key, the table's
  CHECK clauses, whether its key is AUTOINCREMENT, and its indexes' and triggers' SQL. How the CREATE statements were
  written (column order, quoting, a column added later) doesn't count."""
  tables = {}
  for table, sql in execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
    columns = {name: f"{type}{' NOT NULL' if notnull else ''}{' PRIMARY KEY' if pk else ''}"
               for _, name, type, notnull, _, pk, _ in execute(f'PRAGMA table_xinfo("{table}")').fetchall()}
    for _, _, parent, column, target, _, on_delete, _ in execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
      columns[column] += f" REFERENCES {parent} ({target}) ON DELETE {on_delete}"
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
    tables[table] = {"columns": columns, "checks": sorted(checks),
                     "autoincrement": bool(re.search(r"\bAUTOINCREMENT\b", sql, re.IGNORECASE))}
    for kind in ("index", "trigger"):
      tables[table][kind] = {name: " ".join(definition.split()) for name, definition in execute(
          "SELECT name, sql FROM sqlite_master WHERE type = ? AND tbl_name = ? AND sql IS NOT NULL", (kind, table))}
  return tables


def differences(found: dict[str, dict], wanted: dict[str, dict], whose: str = "this version's") -> list[str]:
  """How the schema `found` differs from `wanted`, `whose` schema, each as people read it."""
  lines = [f"table {table} is missing" for table in sorted(wanted.keys() - found.keys())]
  lines += [f"table {table} is not {whose}" for table in sorted(found.keys() - wanted.keys())]
  for table in sorted(found.keys() & wanted.keys()):
    have, want = found[table], wanted[table]
    for kind in ("columns", "index", "trigger"):
      for name in sorted(have[kind].keys() | want[kind].keys()):
        where = f"{table}.{name}" if kind == "columns" else f"{kind} {name} on {table}"
        if name not in have[kind]:
          lines.append(f"{where} is missing")
        elif name not in want[kind]:
          lines.append(f"{where} is not {whose}")
        elif have[kind][name] != want[kind][name]:
          lines.append(f"{where} is {have[kind][name]}, {whose} is {want[kind][name]}")
    if have["checks"] != want["checks"]:
      lines.append(f"{table} has the checks {'; '.join(have['checks']) or 'none'}, "
                   f"{whose} are {'; '.join(want['checks']) or 'none'}")
    if have["autoincrement"] != want["autoincrement"]:
      lines.append(f"{table} has AUTOINCREMENT, {whose} does not" if have["autoincrement"]
                   else f"{table} lacks AUTOINCREMENT, {whose} has it")
  return lines


def built_schema(script: str) -> dict[str, dict]:
  """The schema `script` creates in a throwaway in-memory database."""
  with closing(sqlite3.connect(":memory:")) as built:
    built.executescript(script)
    return describe(built.execute)


def migrate(db: SqliteDatabase, baseline: str, steps: list, latest: str, disposable: bool = False,
            run: str = migration_log.BOOT, trigger: str = "boot"):
  """Brings the file `db` has open to the latest version, `len(steps)`, whose schema is `latest`, and checks it, all in
  one transaction with foreign keys off, ending with the foreign key check. A step is a function of the database that
  may return the manual actions its change leaves (each `where` to run it, the exact `command` or None, and `why`), or
  RECREATE. An empty file is created at the latest version. A file at version 0 must have exactly the `baseline`
  schema, and one above the latest is refused. Any failure rolls the whole run back, `user_version` included, and
  raises `SchemaRefused`; a `disposable` file is recreated empty instead, whether it doesn't fit or its migration
  failed, so it never stops the boot. A file it migrates, recreates or refuses goes in the migration history under
  `run`, which `trigger` (boot, restore) started, a migrated one with its steps' actions in order."""
  foreign_keys = db.execute_sql("PRAGMA foreign_keys").fetchone()[0]
  db.execute_sql("PRAGMA foreign_keys=OFF")
  # Why a disposable file is started again empty after its migration failed.
  failed = None
  try:
    # The pragma is ignored inside a transaction.
    if foreign_keys and db.execute_sql("PRAGMA foreign_keys").fetchone()[0]:
      raise RuntimeError("The schema step must not run inside a transaction: foreign keys can't be turned off.")
    while True:
      try:
        with db.atomic():
          version = db.execute_sql("PRAGMA user_version").fetchone()[0]
          found = describe(db.execute_sql)
          unfit = None
          if found and version > len(steps):
            unfit = f"it is at version {version}, written by a later Sage; this version knows up to {len(steps)}."
          elif found and version == 0 and (unlike := differences(found, built_schema(baseline), f"{BASELINE_RELEASE}'s")):
            unfit = (f"it has no version and isn't {BASELINE_RELEASE}'s ({'; '.join(unlike)}). A database from before "
                     f"{BASELINE_RELEASE} upgrades through it: run Sage {BASELINE_RELEASE} on it once, then this version.")
          if unfit and not disposable:
            raise SchemaRefused(db.database, unfit)
          pending = [] if not found or unfit or failed else list(enumerate(steps[version:], version + 1))
          recreated = failed or unfit or (
              "its migration starts it again" if RECREATE in [step for _, step in pending] else None)
          if not found or recreated:
            if recreated:
              logger.warning(f"Recreating {db.database} empty: {recreated}")
              # A virtual table's own tables go with it.
              for (table,) in db.execute_sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                             "'sqlite_%' ORDER BY sql NOT LIKE 'CREATE VIRTUAL TABLE%'").fetchall():
                db.execute_sql(f'DROP TABLE IF EXISTS "{table}"')
            for statement in statements(latest):
              db.execute_sql(statement)
            pending = []
          actions = []
          for number, step in pending:
            try:
              actions += step(db) or []
            except Exception as e:
              raise SchemaRefused(db.database, f"its migration step {number} failed ({e}).") from e
          db.execute_sql(f"PRAGMA user_version = {len(steps)}")
          if unlike := differences(describe(db.execute_sql), built_schema(latest)):
            raise SchemaRefused(db.database,
                                f"after its migration steps it differs from this version's: {'; '.join(unlike)}.")
          if broken := db.execute_sql("PRAGMA foreign_key_check").fetchall():
            raise IntegrityError("FOREIGN KEY check failed: " + ", ".join(
                f"{table} row {row} points at a missing {parent}" for table, row, parent, _ in broken))
        break
      except (IntegrityError, SchemaRefused) as e:
        refused = e if isinstance(e, SchemaRefused) else SchemaRefused(db.database, f"the data in it refuses the change ({e}).")
        if not disposable or failed:
          migration_log.record(run, trigger, db.database, version, len(steps), "refused", refused.reason)
          if refused is e:
            raise
          raise refused from e
        failed = f"its migration failed: {refused.reason}"
    if recreated:
      migration_log.record(run, trigger, db.database, version, len(steps), "recreated", recreated)
    elif found and version < len(steps):
      logger.info(f"Migrated {db.database} from version {version} to {len(steps)} (Sage {config.VERSION}).")
      migration_log.record(run, trigger, db.database, version, len(steps), "migrated", None, actions)
  finally:
    db.execute_sql(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")


def unsound(error: Exception) -> bool:
  """Whether `error` says the file isn't a sound SQLite database: not a database at all, or a damaged one."""
  cause = error.__cause__ or error.__context__
  return getattr(cause, "sqlite_errorcode", None) in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


def discard(path: Path, error: Exception):
  """Removes the side file at `path` that isn't a sound database (`unsound`), its WAL files with it, so it opens again
  empty, and records why in the migration history."""
  for part in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
    part.unlink(missing_ok=True)
  logger.warning(f"Recreating {path} empty: it was not a sound database ({error}).")
  migration_log.record(migration_log.BOOT, "boot", str(path), None, None, "recreated",
                       f"it was not a sound database ({error}).")
