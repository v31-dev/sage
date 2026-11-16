import contextlib
import logging
import sqlite3

import pytest
from peewee import SqliteDatabase

from services.db.migrator import RECREATE, SchemaRefused, built_schema, describe, migrate, statements
from services.logs import applications
from services.logs import migrations as log_migrations
from services.logs import requests, tasks
from services.logs.store import open_db
from services.metrics import migrations as metrics_migrations
from services.metrics import shards

BASELINE = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "text" TEXT NOT NULL);'
LATEST = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "body" TEXT NOT NULL);'


def renamed(db):
  db.execute_sql('ALTER TABLE "note" RENAME COLUMN "text" TO "body"')


@pytest.fixture(autouse=True)
def history(data_dir):
  """The migration history goes into the test's data directory."""


@pytest.fixture
def file(tmp_path):
  db = SqliteDatabase(tmp_path / "side.db")
  db.connect()
  yield db
  db.close()


def state(db):
  return (describe(db.execute_sql), db.execute_sql("PRAGMA user_version").fetchone()[0],
          [tuple(row) for row in db.execute_sql('SELECT * FROM "note"').fetchall()] if "note" in db.get_tables() else None)


class TestADisposableFile:
  def test_a_file_from_before_its_baseline_is_started_again_empty(self, file, caplog):
    file.execute_sql('CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "old" TEXT)')
    file.execute_sql("INSERT INTO note (old) VALUES ('gone')")

    with caplog.at_level(logging.WARNING, logger="services.db.migrations"):
      migrate(file, BASELINE, [renamed], LATEST, disposable=True)

    assert state(file) == (built_schema(LATEST), 1, [])
    assert "Recreating" in caplog.text and "it has no version and isn't 0.1.3's" in caplog.text

  def test_a_file_from_a_later_sage_is_started_again_empty(self, file):
    file.execute_sql(LATEST)
    file.execute_sql("INSERT INTO note (body) VALUES ('newer')")
    file.execute_sql("PRAGMA user_version = 5")

    migrate(file, BASELINE, [renamed], LATEST, disposable=True)

    assert state(file) == (built_schema(LATEST), 1, [])

  def test_a_failing_step_still_stops_and_leaves_it_as_it_was(self, file):
    file.execute_sql(BASELINE)
    file.execute_sql("INSERT INTO note (text) VALUES ('kept')")
    before = state(file)

    def failing(db):
      renamed(db)
      raise ValueError("broken")

    with pytest.raises(SchemaRefused, match=r"its migration step 1 failed \(broken\)\."):
      migrate(file, BASELINE, [failing], LATEST, disposable=True)
    assert state(file) == before

  def test_a_baseline_file_keeps_its_rows_through_the_steps(self, file):
    file.execute_sql(BASELINE)
    file.execute_sql("INSERT INTO note (text) VALUES ('kept')")

    migrate(file, BASELINE, [renamed], LATEST, disposable=True)

    assert state(file) == (built_schema(LATEST), 1, [(1, "kept")])


class TestRecreate:
  LATER = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "body" TEXT NOT NULL, "seen" INTEGER);'

  def test_the_step_starts_the_file_empty_at_the_latest_version_and_the_later_steps_do_not_run(self, file):
    file.execute_sql(BASELINE)
    file.execute_sql("INSERT INTO note (text) VALUES ('gone')")

    def never(db):
      raise AssertionError("ran")

    migrate(file, BASELINE, [RECREATE, never], self.LATER)

    assert state(file) == (built_schema(self.LATER), 2, [])

  def test_a_file_past_the_step_runs_only_the_steps_after_it(self, file):
    file.execute_sql(LATEST)
    file.execute_sql("INSERT INTO note (body) VALUES ('kept')")
    file.execute_sql("PRAGMA user_version = 1")

    migrate(file, BASELINE, [RECREATE, lambda db: db.execute_sql('ALTER TABLE "note" ADD COLUMN "seen" INTEGER')],
            self.LATER)

    assert state(file) == (built_schema(self.LATER), 2, [(1, "kept", None)])

  def test_a_full_text_index_and_its_own_tables_are_started_again(self, tmp_path):
    db = open_db(tmp_path / "shop-web.db")
    applications.application_schema(db)
    db.execute_sql("INSERT INTO containerlogs (hostname, ts, stream, message) VALUES ('w', 't', 'stdout', 'hello')")
    latest = statements(log_migrations.APPLICATION_BASELINE)

    migrate(db, log_migrations.APPLICATION_BASELINE, [RECREATE], "\n".join(latest))

    assert db.execute_sql("SELECT count(*) FROM containerlogs").fetchone()[0] == 0
    db.execute_sql("INSERT INTO containerlogs (hostname, ts, stream, message) VALUES ('w', 't', 'stdout', 'again')")
    assert db.execute_sql("SELECT rowid FROM containerlogsindex WHERE containerlogsindex MATCH 'again'").fetchall() == [(1,)]
    db.close()


def test_a_trigger_that_differs_is_refused(file):
  latest = LATEST + "\nCREATE TRIGGER note_ai AFTER INSERT ON note BEGIN SELECT 1; END;"
  file.execute_sql(LATEST)
  file.execute_sql("CREATE TRIGGER note_ai AFTER INSERT ON note BEGIN SELECT 2; END")
  file.execute_sql("PRAGMA user_version = 1")

  with pytest.raises(SchemaRefused, match="trigger note_ai on note is CREATE TRIGGER note_ai AFTER INSERT ON note BEGIN "
                                          "SELECT 2; END, this version's is"):
    migrate(file, BASELINE, [renamed], latest)


@pytest.mark.parametrize("before", [0, 1], ids=["off", "on"])
def test_the_files_foreign_keys_setting_is_kept(file, before):
  file.execute_sql(f"PRAGMA foreign_keys = {before}")
  migrate(file, BASELINE, [renamed], LATEST)
  assert file.execute_sql("PRAGMA foreign_keys").fetchone()[0] == before


def test_a_script_is_split_into_its_statements_with_a_triggers_body_whole():
  assert statements(applications.TRIGGERS)[0].endswith("VALUES (new.id, new.message);\nEND;")
  assert len(statements(applications.TRIGGERS + LATEST)) == 3


SIDE_FILES = {
    "an application's logs": (
        (log_migrations.APPLICATION_BASELINE,
         log_migrations.APPLICATION_STEPS),
        lambda path: applications.application_schema(
            open_db(path)).db,
        ("INSERT INTO containerlogs (hostname, ts, stream, message) VALUES ('w1', '2026-01-01T00:00:00Z', 'stdout', 'hello')",
         )),
    "the requests": (
        (log_migrations.REQUESTS_BASELINE,
         log_migrations.REQUESTS_STEPS),
        lambda path: requests.requests_schema(
            open_db(path)).db,
        ("INSERT INTO requests (ts, worker, host, method, path, status, duration_ms, router, service, client, size) "
         "VALUES ('t', 'w1', 'shop.test', 'GET', '/', 200, 1.5, 'r', 's', 'c', 10)",
         )),
    "the task lines": (
        (log_migrations.TASKS_BASELINE,
         log_migrations.TASKS_STEPS),
        lambda path: tasks.task_lines_schema(
            open_db(path)).db,
        ("INSERT INTO tasklogs (task_id, sub_task_id, ts, level, logger, message) VALUES ('t1', '', 't', 'INFO', 'l', 'hi')",
         )),
    "a host's metrics": (
        (metrics_migrations.BASELINE,
         metrics_migrations.STEPS),
        lambda path: shards.open_shard(
            str(path))["db"],
        ("INSERT INTO workermeta (id, cpu_cores, mem_total_mb, disk_total_gb) VALUES (1, 2, 2048, 20.0)",
         )),
}


@pytest.mark.parametrize("kind", SIDE_FILES)
def test_a_baseline_side_file_through_every_step_equals_a_new_one_and_keeps_its_rows(tmp_path, kind):
  (baseline, steps), opened, rows = SIDE_FILES[kind]
  fresh = opened(tmp_path / "new.db")
  wanted = describe(fresh.execute_sql), fresh.execute_sql("PRAGMA user_version").fetchone()[0]
  fresh.close()
  with contextlib.closing(sqlite3.connect(tmp_path / "old.db")) as old:
    old.executescript(baseline)
    for row in rows:
      old.execute(row)
    old.commit()

  migrated = opened(tmp_path / "old.db")

  assert (describe(migrated.execute_sql), migrated.execute_sql("PRAGMA user_version").fetchone()[0]) == wanted
  assert wanted[1] == len(steps)
  (table,) = [name for name in describe(migrated.execute_sql) if name in rows[0]]
  assert migrated.execute_sql(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 1
  migrated.close()


class TestTheBootPass:
  def test_every_log_file_is_migrated_when_the_logs_start(self, data_dir):
    from services.logs import Logs
    (data_dir / "logs").mkdir()
    with contextlib.closing(sqlite3.connect(data_dir / "logs" / "tasks.db")) as old:
      old.executescript(log_migrations.TASKS_BASELINE)
      old.execute("INSERT INTO tasklogs (task_id, sub_task_id, ts, level, logger, message) VALUES ('t1', '', 't', 'INFO', 'l', 'kept')")
      old.commit()

    logs = Logs()

    with contextlib.closing(sqlite3.connect(data_dir / "logs" / "tasks.db")) as migrated:
      assert migrated.execute("PRAGMA user_version").fetchone()[0] == len(log_migrations.TASKS_STEPS)
      assert migrated.execute("SELECT task_id, message FROM tasklogs").fetchall() == [("t1", "kept")]
    assert logs._stores == {}

  def test_a_failing_step_stops_the_logs_from_starting(self, data_dir, monkeypatch):
    from services.logs import Logs
    (data_dir / "logs").mkdir()
    with contextlib.closing(sqlite3.connect(data_dir / "logs" / "tasks.db")) as old:
      old.executescript(log_migrations.TASKS_BASELINE)

    def failing(db):
      raise ValueError("broken")

    monkeypatch.setattr(log_migrations, "TASKS_STEPS", [failing])
    with pytest.raises(Exception, match="its migration step 1 failed"):
      Logs()

  def test_every_metrics_file_is_migrated_when_the_metrics_start(self, data_dir):
    from services.metrics import Metrics
    (data_dir / "metrics").mkdir()
    with contextlib.closing(sqlite3.connect(data_dir / "metrics" / "w1.db")) as old:
      old.execute("CREATE TABLE workermeta (id INTEGER PRIMARY KEY, cores INTEGER)")
      old.execute("INSERT INTO workermeta VALUES (1, 2)")
      old.commit()

    Metrics()

    with contextlib.closing(sqlite3.connect(data_dir / "metrics" / "w1.db")) as migrated:
      assert describe(migrated.execute) == built_schema(metrics_migrations.BASELINE)
      assert migrated.execute("SELECT count(*) FROM workermeta").fetchone()[0] == 0
