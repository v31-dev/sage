import contextlib
import json
import logging
import sqlite3

import pytest
from peewee import SqliteDatabase

import config
from services.db import migration_log
from services.db.migration_log import migration_runs, record
from services.db.migrator import RECREATE, SchemaRefused, migrate

BASELINE = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "text" TEXT NOT NULL);'
LATEST = 'CREATE TABLE "note" ("id" INTEGER NOT NULL PRIMARY KEY, "body" TEXT NOT NULL);'


def renamed(db):
  db.execute_sql('ALTER TABLE "note" RENAME COLUMN "text" TO "body"')


@pytest.fixture
def file(data_dir):
  db = SqliteDatabase(data_dir / "logs" / "side.db")
  (data_dir / "logs").mkdir()
  db.connect()
  yield db
  db.close()


def entries():
  path = config.DATA_DIR / "migrations.log"
  return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class TestTheRunnerRecords:
  def test_a_migrated_file_under_this_boot_with_its_versions_and_release(self, file, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "1.2.3")
    file.execute_sql(BASELINE)

    migrate(file, BASELINE, [renamed], LATEST)

    [entry] = entries()
    assert {key: entry[key] for key in ("run", "trigger", "release", "file", "from", "to", "outcome", "detail")} == {
        "run": migration_log.BOOT, "trigger": "boot", "release": "1.2.3", "file": "logs/side.db", "from": 0, "to": 1,
        "outcome": "migrated", "detail": None}

  def test_nothing_for_a_new_file_or_one_already_current(self, file):
    migrate(file, BASELINE, [renamed], LATEST)
    migrate(file, BASELINE, [renamed], LATEST)
    assert entries() == []

  def test_a_refused_run_though_its_database_was_rolled_back(self, file):
    file.execute_sql(BASELINE)

    def failing(db):
      raise ValueError("broken")

    with pytest.raises(SchemaRefused):
      migrate(file, BASELINE, [failing], LATEST, run="r1", trigger="restore")

    [entry] = entries()
    assert (entry["run"], entry["trigger"], entry["outcome"], entry["detail"], entry["from"], entry["to"]) == (
        "r1", "restore", "refused", "its migration step 1 failed (broken).", 0, 1)
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

    [entry] = entries()
    assert entry["outcome"] == "refused" and entry["detail"].startswith("the data in it refuses the change (FOREIGN KEY")

  @pytest.mark.parametrize("setup, detail", [
      (["CREATE TABLE note (id INTEGER PRIMARY KEY, old TEXT)"], "it has no version and isn't 0.1.3's"),
      ([BASELINE], "its migration starts it again"),
  ], ids=["a file that doesn't fit", "a RECREATE step"])
  def test_a_recreated_file_with_why(self, file, setup, detail):
    for statement in setup:
      file.execute_sql(statement)

    migrate(file, BASELINE, [RECREATE], LATEST, disposable=True)

    [entry] = entries()
    assert entry["outcome"] == "recreated" and entry["detail"].startswith(detail)

  def test_a_history_it_cannot_write_is_a_warning_and_the_migration_goes_through(self, file, monkeypatch, caplog):
    (config.DATA_DIR / "migrations.log").mkdir()
    file.execute_sql(BASELINE)

    with caplog.at_level(logging.WARNING, logger="services.db.migration_log"):
      migrate(file, BASELINE, [renamed], LATEST)

    assert "Could not write the migration history" in caplog.text
    assert file.execute_sql("PRAGMA user_version").fetchone()[0] == 1


class TestAPlatformRestore:
  def test_its_install_is_a_run_of_its_own(self, db, tmp_path):
    from services.db.database import Database
    Database().snapshot(tmp_path / "snapshot.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "snapshot.db")) as copy:
      copy.execute("PRAGMA user_version = 99")
      copy.commit()

    with pytest.raises(SchemaRefused):
      Database().install(tmp_path / "snapshot.db", {})

    [entry] = entries()
    assert (entry["trigger"], entry["file"], entry["outcome"]) == ("restore", "data.db", "refused")
    assert entry["run"] != migration_log.BOOT


class TestRuns:
  def test_none_without_a_history(self, data_dir):
    assert migration_runs() == []

  def test_each_run_with_its_files_in_order_newest_first_and_its_overall_outcome(self, data_dir, monkeypatch):
    times = iter(["2026-01-01T00:00:00", "2026-01-01T00:00:01", "2026-02-01T00:00:00", "2026-02-01T00:00:01"])
    monkeypatch.setattr(migration_log, "utcnow", lambda: type("Now", (), {"isoformat": lambda self: next(times)})())
    record("a", "boot", str(data_dir / "data.db"), 1, 2, "migrated", None)
    record("a", "boot", str(data_dir / "logs" / "tasks.db"), 0, 1, "recreated", "it is at version 5")
    record("b", "boot", str(data_dir / "data.db"), 2, 3, "refused", "its migration step 3 failed (x).")
    record("b", "boot", str(data_dir / "logs" / "requests.db"), 0, 1, "migrated", None)

    runs = migration_runs()

    assert [(run["run"], run["at"], run["outcome"]) for run in runs] == [
        ("b", "2026-02-01T00:00:00", "refused"), ("a", "2026-01-01T00:00:00", "recreated")]
    assert [(file["file"], file["from"], file["to"], file["outcome"]) for file in runs[1]["files"]] == [
        ("data.db", 1, 2, "migrated"), ("logs/tasks.db", 0, 1, "recreated")]

  def test_a_line_cut_off_or_not_an_entry_is_skipped(self, data_dir):
    record("a", "boot", str(data_dir / "data.db"), 1, 2, "migrated", None)
    with open(data_dir / "migrations.log", "a") as log:
      log.write('{"run": "b", "trigger": "boot"}\n[1, 2]\n{"run": "c", "trig')

    assert [run["run"] for run in migration_runs()] == ["a"]


class TestThisProcesssRuns:
  def test_none_before_a_run_records(self, data_dir):
    from services.db.migration_log import MigrationRuns
    assert MigrationRuns().latest() is None

  def test_the_latest_run_with_its_worst_outcome_and_every_action_it_left(self, data_dir, monkeypatch):
    from services.db.migration_log import MigrationRuns
    monkeypatch.setattr(config, "VERSION", "0.2.0")
    record("boot", "boot", str(data_dir / "data.db"), 0, 1, "migrated", None, [{"where": "Sage"}, {"where": "Cloudflare"}])
    record("boot", "boot", str(data_dir / "logs" / "tasks.db"), 0, 1, "recreated", "it is at version 5")
    assert MigrationRuns().latest() == {"run": "boot", "trigger": "boot", "release": "0.2.0", "outcome": "recreated",
                                        "actions": 2}
    record("restore", "restore", str(data_dir / "data.db"), 1, 1, "migrated", None, [])
    record("boot", "boot", str(data_dir / "logs" / "w1.db"), 0, 1, "recreated", "not sound")
    assert MigrationRuns().latest()["run"] == "restore"
