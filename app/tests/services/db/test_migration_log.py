import contextlib
import json
import logging
import sqlite3
import threading

import pytest
from peewee import SqliteDatabase

import config
from services.db import migration_log
from services.db.migration_log import MigrationRuns, migration_runs, record
from services.db.migrator import RECREATE, SchemaRefused, migrate

BASELINE = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "text" TEXT NOT NULL);'
LATEST = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "body" TEXT NOT NULL);'


def renamed(db):
  db.execute_sql('ALTER TABLE "note" RENAME COLUMN "text" TO "body"')


@pytest.fixture
def file(data_dir, caplog):
  """A side file under the data directory, migrated with the log at INFO as production has it."""
  caplog.set_level(logging.INFO)
  (data_dir / "logs").mkdir()
  db = SqliteDatabase(data_dir / "logs" / "side.db")
  db.connect()
  yield db
  db.close()


def messages(run):
  return [(line["level"], line["message"]) for line in run["lines"]]


class TestTheRunnerLogs:
  def test_a_migrated_file_under_this_boot_with_the_release_and_what_it_logged(self, file, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "1.2.3")
    file.execute_sql(BASELINE)

    migrate(file, BASELINE, [renamed], LATEST)

    [run] = migration_runs()
    assert (run["run"], run["trigger"], run["release"], run["refused"], run["actions"]) == (
        migration_log.BOOT, "boot", "1.2.3", False, 0)
    assert messages(run) == [("INFO", f"Migrated [{file.database}] from version [0] to [1] (Sage [1.2.3]).")]
    assert (config.DATA_DIR / "migrations" / f"migration-{migration_log.BOOT}.log").exists()

  def test_nothing_for_a_new_file_or_one_already_current(self, file):
    migrate(file, BASELINE, [renamed], LATEST)
    migrate(file, BASELINE, [renamed], LATEST)
    assert migration_runs() == []

  def test_a_refused_run_though_its_database_was_rolled_back(self, file):
    file.execute_sql(BASELINE)

    def failing(db):
      raise ValueError("broken")

    with pytest.raises(SchemaRefused):
      migrate(file, BASELINE, [failing], LATEST, run="r1", trigger="restore")

    [run] = migration_runs()
    assert (run["run"], run["trigger"], run["refused"]) == ("r1", "restore", True)
    assert messages(run)[-1] == ("ERROR", f"Refused [{file.database}] at version [0]: its migration step 1 failed (broken).")
    assert file.execute_sql("PRAGMA user_version").fetchone()[0] == 0

  def test_data_that_refuses_the_change(self, file):
    file.execute_sql(LATEST)
    file.execute_sql("PRAGMA user_version = 1")

    def orphan(db):
      db.execute_sql('CREATE TABLE "child" ("id" INTEGER PRIMARY KEY, "note_id" INTEGER REFERENCES "note" ("id"))')
      db.execute_sql('INSERT INTO "child" ("note_id") VALUES (7)')

    latest = LATEST + '\nCREATE TABLE "child" ("id" INTEGER PRIMARY KEY, "note_id" INTEGER REFERENCES "note" ("id"));'
    with pytest.raises(SchemaRefused, match="FOREIGN KEY check failed"):
      migrate(file, BASELINE, [renamed, orphan], latest)

    [run] = migration_runs()
    level, message = messages(run)[-1]
    assert level == "ERROR" and "the data in it refuses the change (FOREIGN KEY" in message

  @pytest.mark.parametrize("setup, detail", [
      (["CREATE TABLE note (id INTEGER PRIMARY KEY, old TEXT)"], "it has no version and isn't 0.1.3's"),
      ([BASELINE], "its migration starts it again"),
  ], ids=["a file that doesn't fit", "a RECREATE step"])
  def test_a_recreated_file_with_why(self, file, setup, detail):
    for statement in setup:
      file.execute_sql(statement)

    migrate(file, BASELINE, [RECREATE], LATEST, disposable=True)

    [run] = migration_runs()
    level, message = messages(run)[0]
    assert level == "WARNING" and message.startswith(f"Recreating [{file.database}] empty: {detail}")

  def test_a_log_it_cannot_write_is_a_warning_and_the_migration_goes_through(self, file, caplog):
    (config.DATA_DIR / "migrations").write_text("not a folder")
    file.execute_sql(BASELINE)

    migrate(file, BASELINE, [renamed], LATEST)

    assert "Could not write the migration log" in caplog.text
    assert file.execute_sql("PRAGMA user_version").fetchone()[0] == 1

  def test_a_file_keeps_what_its_thread_logged_and_not_what_others_did_meanwhile(self, file):
    file.execute_sql(BASELINE)
    step_logger = logging.getLogger("services.db.migrations")

    def logging_step(db):
      step_logger.info("Renamed [text] to [body].")
      other = threading.Thread(target=lambda: step_logger.info("Another thread's line."))
      other.start()
      other.join()
      renamed(db)

    migrate(file, BASELINE, [logging_step], LATEST)

    [run] = migration_runs()
    assert [message for _, message in messages(run)] == [
        "Renamed [text] to [body].", f"Migrated [{file.database}] from version [0] to [1] (Sage [{config.VERSION}])."]

  def test_nothing_is_left_listening_once_the_file_is_migrated(self, file):
    before = list(logging.getLogger().handlers)
    file.execute_sql(BASELINE)
    migrate(file, BASELINE, [renamed], LATEST)
    with pytest.raises(SchemaRefused):
      migrate(file, BASELINE, [renamed, lambda db: (_ for _ in ()).throw(ValueError("x"))], LATEST)
    assert logging.getLogger().handlers == before


class TestAPlatformRestore:
  def test_its_install_is_a_run_of_its_own(self, db, tmp_path):
    from services.db.database import Database
    Database().snapshot(tmp_path / "snapshot.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "snapshot.db")) as copy:
      copy.execute("PRAGMA user_version = 99")
      copy.commit()

    with pytest.raises(SchemaRefused):
      Database().install(tmp_path / "snapshot.db", {})

    [run] = migration_runs()
    assert (run["trigger"], run["refused"]) == ("restore", True)
    assert run["run"] != migration_log.BOOT


class TestRuns:
  def test_none_without_a_log(self, data_dir):
    assert migration_runs() == []

  def test_each_run_in_a_file_of_its_own_newest_first_its_actions_last_and_counted(self, data_dir, monkeypatch):
    times = iter(["2026-01-01T00:00:00", "2026-01-01T00:00:01", "2026-02-01T00:00:00"])
    monkeypatch.setattr(migration_log, "utcnow", lambda: type("Now", (), {"isoformat": lambda self: next(times)})())
    line = {"ts": "2026-01-01T00:00:00", "level": "INFO", "message": "Migrated [data.db]."}
    record("a", "boot", "migrated", [line], [{"where": "Worker [w1]", "command": "mv a b", "why": "move [a]."},
                                             {"where": "Sage", "command": None, "why": "Add [shop] again."}])
    record("a", "boot", "recreated", [{**line, "level": "WARNING", "message": "Recreating [tasks.db] empty."}])
    record("b", "boot", "refused", [{**line, "level": "ERROR", "message": "Refused [data.db]."}])

    assert sorted(path.name for path in (data_dir / "migrations").iterdir()) == ["migration-a.log", "migration-b.log"]
    first, second = migration_runs()
    assert (first["run"], first["refused"], first["actions"]) == ("b", True, 0)
    assert (second["run"], second["at"], second["refused"], second["actions"]) == ("a", "2026-01-01T00:00:00", False, 2)
    assert messages(second) == [
        ("INFO", "Migrated [data.db]."), ("WARNING", "Recreating [tasks.db] empty."),
        ("CRITICAL", "Worker [w1] - Run command to move [a].\n\nmv a b"), ("CRITICAL", "Sage - Add [shop] again.")]

  def test_a_line_cut_off_or_a_log_without_its_first_line_is_skipped(self, data_dir):
    record("a", "boot", "migrated", [{"ts": "t", "level": "INFO", "message": "kept"}])
    with open(data_dir / "migrations" / "migration-a.log", "a") as log:
      log.write('[1, 2]\n{"ts": "t", "lev')
    (data_dir / "migrations" / "migration-b.log").write_text(json.dumps({"ts": "t", "level": "INFO", "message": "x"}) + "\n")

    assert [(run["run"], messages(run)) for run in migration_runs()] == [("a", [("INFO", "kept")])]


class TestThisProcesssRuns:
  def test_none_before_a_run_records(self, data_dir):
    assert MigrationRuns().latest() is None

  def test_the_latest_run_with_its_worst_outcome_and_every_action_it_left(self, data_dir, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "0.2.0")
    record("boot", "boot", "migrated", [], [{"where": "Sage", "command": None, "why": "x"},
                                            {"where": "Cloudflare", "command": None, "why": "y"}])
    record("boot", "boot", "recreated", [])
    assert MigrationRuns().latest() == {"run": "boot", "trigger": "boot", "release": "0.2.0", "outcome": "recreated",
                                        "actions": 2}
    record("restore", "restore", "migrated", [])
    record("boot", "boot", "recreated", [])
    assert MigrationRuns().latest()["run"] == "restore"
