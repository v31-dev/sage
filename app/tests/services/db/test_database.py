import contextlib
import logging
import sqlite3

import pytest

import config
from services.db import migrations
from services.db.database import MODELS, Database
from services.db.migration_log import MigrationRuns
from services.db.migrator import SchemaRefused
from services.db.models import Backup, BaseEvent, BaseModel, Container, ContainerEvent, Instance, Project, Setting, Worker, WorkerEvent
from services.db.models import db as database

ORPHAN_DOMAIN = [
    "PRAGMA foreign_keys=OFF",
    "INSERT INTO domain (id, container_id, name, type, port, created_at, updated_at) "
    "VALUES ('ghost', 'missing', 'ghost', 'public', 80, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
    "PRAGMA foreign_keys=ON",
]


def columns(db, table):
  return {column.name for column in db.get_columns(table)}


def counts(db):
  return {table: db.execute_sql(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in db.get_tables()}


def schema(db):
  return db.execute_sql("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def point_a_domain_at_a_missing_container(db):
  for statement in ORPHAN_DOMAIN:
    db.execute_sql(statement)


def rewrite_table(db, table, old, new):
  """Rebuilds `table` from its own CREATE statement with `old` replaced by `new`, keeping its rows and indexes."""
  (sql,) = db.execute_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
  assert old in sql
  indexes = [row[0] for row in db.execute_sql(
      "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (table,))]
  db.execute_sql("PRAGMA foreign_keys=OFF")
  db.execute_sql(sql.replace(old, new).replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{table}_new"', 1))
  db.execute_sql(f'INSERT INTO "{table}_new" SELECT * FROM "{table}"')
  db.execute_sql(f'DROP TABLE "{table}"')
  db.execute_sql(f'ALTER TABLE "{table}_new" RENAME TO "{table}"')
  for index in indexes:
    db.execute_sql(index)
  db.execute_sql("PRAGMA foreign_keys=ON")


def version(db):
  return db.execute_sql("PRAGMA user_version").fetchone()[0]


@pytest.fixture
def baseline(data_dir, monkeypatch):
  """A 0.1.3 database at the data path, not opened yet."""
  from services.db import encryption
  monkeypatch.setattr(encryption, "_cipher", None)
  encryption.initialize_encryption(__import__("os").environ["ENCRYPTION_KEY"])
  with contextlib.closing(sqlite3.connect(config.DB_PATH)) as file:
    file.executescript(migrations.BASELINE)
  yield config.DB_PATH
  database.close()


def file_schema(path):
  with contextlib.closing(sqlite3.connect(path)) as file:
    return file.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(), file.execute(
        "PRAGMA user_version").fetchone()[0]


def test_opens_in_the_data_dir(db, data_dir):
  assert (data_dir / "data.db").exists()


def test_creates_the_data_dir_when_it_is_missing(data_dir, monkeypatch):
  monkeypatch.setattr(config, "DB_PATH", data_dir / "not" / "yet" / "data.db")
  Database()
  database.close()
  assert config.DB_PATH.exists()


def test_every_model_is_registered_and_has_a_table(db):
  assert set(MODELS) == set(BaseModel.__subclasses__()) - {BaseEvent} | set(BaseEvent.__subclasses__())
  assert set(db.get_tables()) == {model._meta.table_name for model in MODELS} | {"sqlite_sequence"}


@pytest.mark.parametrize("pragma, expected", [
    ("foreign_keys", 1),
    ("journal_mode", "wal"),
    ("busy_timeout", 5000),
    ("cache_size", -64000),
])
def test_applies_the_connection_pragmas(db, pragma, expected):
  assert db.execute_sql(f"PRAGMA {pragma}").fetchone()[0] == expected


class TestInstance:
  def test_a_new_database_has_one_instance_with_an_id_and_a_generation(self, db):
    (instance,) = Instance.select()
    assert len(instance.id) == len(instance.state_generation) == 16
    assert instance.id != instance.state_generation

  def test_the_instance_and_its_generation_survive_migrating_and_a_restart(self, db):
    before = Instance.get()
    Database().migrate()
    Database()
    assert [(instance.id, instance.state_generation) for instance in Instance.select()] == [
        (before.id, before.state_generation)]

  def test_a_database_without_an_instance_gets_one(self, db):
    Instance.delete().execute()
    Database().migrate()
    assert Instance.select().count() == 1


class TestVersions:
  def test_a_new_database_is_created_at_the_latest_version(self, db):
    assert version(db) == len(migrations.STEPS)

  def test_a_current_database_is_left_alone(self, db, caplog):
    Project.create(name="demo", label="Demo")
    before = schema(db)
    with caplog.at_level(logging.INFO, logger="services.db.migrations"):
      Database().migrate()
    assert schema(db) == before
    assert "Migrated" not in caplog.text
    assert Project.get_by_id("demo").label == "Demo"

  def test_a_baseline_database_is_brought_to_this_versions_schema(self, baseline):
    Database()
    assert version(database) == len(migrations.STEPS)

  def test_a_boot_that_migrates_keeps_a_copy_from_before(self, baseline):
    before = file_schema(baseline)
    Database()
    assert file_schema(baseline.with_name("data.db.migration-0")) == before

  def test_a_database_from_before_the_baseline_is_refused_with_its_way_forward_and_left_as_it_was(self, baseline, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "1.2.3")
    with contextlib.closing(sqlite3.connect(baseline)) as file:
      file.execute("ALTER TABLE worker ADD COLUMN online INTEGER NOT NULL DEFAULT 0")
      file.execute("ALTER TABLE task DROP COLUMN started_at")
    before = file_schema(baseline)

    with pytest.raises(Exception) as refused:
      Database()

    message = str(refused.value)
    assert isinstance(refused.value.__context__, SchemaRefused)
    assert f"Sage 1.2.3 can't update the schema of {config.DB_PATH}: it has no version and isn't 0.1.3's (" in message
    assert "task.started_at is missing; worker.online is not 0.1.3's)" in message
    assert "run Sage 0.1.3 on it once, then this version." in message
    assert message.endswith("How to fix it: https://github.com/v31-dev/sage/releases/tag/v1.2.3")
    database.close()
    assert file_schema(baseline) == before

  def test_a_baseline_written_another_way_is_still_the_baseline(self, baseline):
    """0.1.3 upgraded the databases it found by adding and dropping columns: one it added sits at the end of its table
    and in the table's SQL differently from a fresh install."""
    with contextlib.closing(sqlite3.connect(baseline)) as file:
      file.execute("ALTER TABLE task DROP COLUMN started_at")
      file.execute("ALTER TABLE task ADD COLUMN started_at DATETIME")
      file.commit()
    Database()
    assert version(database) == len(migrations.STEPS)

  def test_a_database_from_a_later_sage_is_refused_and_left_as_it_was(self, db):
    db.execute_sql(f"PRAGMA user_version = {len(migrations.STEPS) + 1}")
    before = schema(db)
    with pytest.raises(SchemaRefused, match=rf"it is at version {len(migrations.STEPS) + 1}, written by a later Sage; "
                       rf"this version knows up to {len(migrations.STEPS)}\."):
      Database().migrate()
    assert (schema(db), version(db)) == (before, len(migrations.STEPS) + 1)

  def test_a_failing_step_leaves_the_database_at_its_version(self, db, monkeypatch):
    Project.create(name="demo", label="Demo")

    def failing(db):
      db.execute_sql("ALTER TABLE project DROP COLUMN description")
      raise ValueError("no such thing")

    monkeypatch.setattr(migrations, "STEPS", [*migrations.STEPS, failing])
    before = schema(db)
    with pytest.raises(SchemaRefused, match=rf"its migration step {len(migrations.STEPS)} failed \(no such thing\)\."):
      Database().migrate()
    assert (schema(db), version(db)) == (before, len(migrations.STEPS) - 1)
    assert Project.get_by_id("demo").label == "Demo"

  def test_a_step_runs_once_and_carries_the_rows(self, db, monkeypatch):
    Project.create(name="demo", label="Demo", description="Shop")
    db.execute_sql("ALTER TABLE project DROP COLUMN description")
    db.execute_sql("ALTER TABLE project ADD COLUMN summary VARCHAR(255)")
    db.execute_sql("UPDATE project SET summary = 'Shop'")
    runs = []

    def renames(db):
      runs.append(1)
      db.execute_sql("ALTER TABLE project RENAME COLUMN summary TO description")

    monkeypatch.setattr(migrations, "STEPS", [*migrations.STEPS, renames])
    Database().migrate()
    Database().migrate()

    assert (runs, version(db)) == ([1], len(migrations.STEPS))
    assert Project.get_by_id("demo").description == "Shop"


def test_a_main_database_that_is_not_sound_stops_the_start_and_stays(data_dir):
  config.DB_PATH.write_bytes(b"not a database at all" * 200)
  with pytest.raises(Exception, match="file is not a database"):
    Database()
  database.close()
  assert config.DB_PATH.read_bytes().startswith(b"not a database at all")


class TestASchemaThatDiffersIsRefused:
  @pytest.mark.parametrize("table, old, new, difference", [
      ("container", '"id" VARCHAR(255) NOT NULL PRIMARY KEY', '"id" INTEGER NOT NULL PRIMARY KEY',
       "container.id is INTEGER NOT NULL PRIMARY KEY, this version's is VARCHAR(255) NOT NULL PRIMARY KEY"),
      ("project", '"description" VARCHAR(255)', '"description" VARCHAR(255) NOT NULL',
       "project.description is VARCHAR(255) NOT NULL, this version's is VARCHAR(255)"),
      ("setting", '"key" VARCHAR(255) NOT NULL PRIMARY KEY', '"key" VARCHAR(255) NOT NULL',
       "setting.key is VARCHAR(255) NOT NULL, this version's is VARCHAR(255) NOT NULL PRIMARY KEY"),
      ("volume", 'REFERENCES "application" ("id") ON DELETE CASCADE', 'REFERENCES "application" ("id")',
       "volume.application_id is INTEGER NOT NULL REFERENCES application (id) ON DELETE NO ACTION, "
       "this version's is INTEGER NOT NULL REFERENCES application (id) ON DELETE CASCADE"),
      ("application", """ CHECK ("type" IN ('docker', 'git'))""", "",
       """application has the checks CHECK (container_count >= 0), this version's are """
       """CHECK ("type" IN ('docker', 'git')); CHECK (container_count >= 0)"""),
      ("task", "'completed', 'failed', 'cancelled'", "'completed', 'failed'",
       """task has the checks CHECK ("status" IN ('completed', 'failed')), """
       """this version's are CHECK ("status" IN ('completed', 'failed', 'cancelled'))"""),
      ("application", "AUTOINCREMENT", "", "application lacks AUTOINCREMENT, this version's has it"),
  ], ids=["type", "NOT NULL", "primary key", "foreign key", "a missing CHECK", "a new choice value", "AUTOINCREMENT"])
  def test_a_changed_definition_stops_the_startup_saying_what_differs_and_leaves_the_database_as_it_was(
          self, db, monkeypatch, table, old, new, difference):
    monkeypatch.setattr(config, "VERSION", "1.2.3")
    rewrite_table(db, table, old, new)
    before = schema(db)

    with pytest.raises(SchemaRefused) as refused:
      Database().migrate()

    message = str(refused.value)
    assert message.startswith(f"Sage 1.2.3 can't update the schema of {config.DB_PATH}: "
                              "after its migration steps it differs from this version's: ")
    assert difference in message
    assert message.endswith("The database was left as it was. "
                            "How to fix it: https://github.com/v31-dev/sage/releases/tag/v1.2.3")
    assert schema(db) == before

  @pytest.mark.parametrize("statement, difference", [
      ("ALTER TABLE project DROP COLUMN description", "project.description is missing"),
      ("ALTER TABLE project ADD COLUMN retired INTEGER", "project.retired is not this version's"),
      ("DROP INDEX domain_name_namespace_worker", "index domain_name_namespace_worker on domain is missing"),
      ("CREATE INDEX project_label ON project (label)", "index project_label on project is not this version's"),
      ("DROP TABLE notification", "table notification is missing"),
      ("CREATE TABLE retired (id INTEGER)", "table retired is not this version's"),
  ], ids=["a missing column", "an extra column", "a missing index", "an extra index", "a missing table", "an extra table"])
  def test_nothing_is_added_or_dropped_to_make_it_fit(self, db, statement, difference):
    db.execute_sql(statement)
    before = schema(db)
    with pytest.raises(SchemaRefused, match=difference):
      Database().migrate()
    assert schema(db) == before

  def test_every_difference_is_named(self, db):
    rewrite_table(db, "container", '"id" VARCHAR(255) NOT NULL PRIMARY KEY', '"id" INTEGER NOT NULL PRIMARY KEY')
    rewrite_table(db, "domain", '"container_id" VARCHAR(255)', '"container_id" INTEGER')

    with pytest.raises(SchemaRefused) as refused:
      Database().migrate()

    assert "container.id is INTEGER" in str(refused.value)
    assert "domain.container_id is INTEGER REFERENCES container (id) ON DELETE SET NULL" in str(refused.value)

  def test_rows_that_point_at_nothing_are_refused_and_named(self, db, application):
    point_a_domain_at_a_missing_container(db)

    with pytest.raises(SchemaRefused, match=r"FOREIGN KEY check failed: domain row \d+ points at a missing container"):
      Database().migrate()


@pytest.mark.parametrize("setup, error", [
    ([], None),
    (ORPHAN_DOMAIN, SchemaRefused),
    (["ALTER TABLE project DROP COLUMN label"], SchemaRefused),
], ids=["after a success", "after refused data", "after a refused schema"])
def test_foreign_keys_are_on_again(db, application, setup, error):
  for statement in setup:
    db.execute_sql(statement)

  with pytest.raises(error) if error else contextlib.nullcontext():
    Database().migrate()

  assert db.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1


def test_it_refuses_to_run_inside_a_transaction_where_foreign_keys_cannot_be_turned_off(db, monkeypatch):
  monkeypatch.setattr(migrations, "STEPS", [*migrations.STEPS, lambda db: db.execute_sql("DROP TABLE notification")])

  with db.atomic():
    with pytest.raises(RuntimeError, match="must not run inside a transaction"):
      Database().migrate()

  assert "notification" in db.get_tables()
  assert db.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1


class TestPlatformCopies:
  @pytest.fixture
  def live(self, db, application, worker):
    """A configuration with its history: a container with an event, a worker event, a notification, a task, a
    backup row."""
    from services.db.models import Notification, Task
    container = Container.create(application=application, worker=worker)
    ContainerEvent.create(container=container, subject_id=container.id, action="deploy", transition="admitted",
                          application_name="demo-web", worker_hostname=worker.hostname)
    WorkerEvent.create(worker=worker, subject_id=worker.hostname, action="add", transition="admitted")
    Notification.create(content="Deployed.")
    Task.create(task_id="t1", name="deploy", executor="app", status="completed")
    Backup.create(s3_path="base/backups/platform/x.db.enc", storage_id="s1", type="platform")
    return container

  def tables(self, path):
    with contextlib.closing(sqlite3.connect(path)) as copy:
      return {table: copy.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
              for (table,) in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

  def test_a_snapshot_holds_the_configuration_without_its_history_or_its_backup_rows(self, live, tmp_path):
    Database().snapshot(tmp_path / "snapshot.db")
    held = self.tables(tmp_path / "snapshot.db")
    assert {table: held[table] for table in ("container", "worker", "application", "project", "instance")} == {
        "container": 1, "worker": 1, "application": 1, "project": 1, "instance": 1}
    assert {table: held[table] for table in ("container_event", "worker_event", "notification", "task", "backup")} == dict.fromkeys(
        ("container_event", "worker_event", "notification", "task", "backup"), 0)
    assert ContainerEvent.select().count() == 1 and Backup.select().count() == 1

  def test_a_snapshot_holds_no_containers_intent(self, live, tmp_path):
    Container.update(intent="deploy").execute()
    Database().snapshot(tmp_path / "snapshot.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "snapshot.db")) as copy:
      assert copy.execute("SELECT intent FROM container").fetchall() == [(None,)]
    assert Container.get_by_id(live.id).intent == "deploy"

  def test_a_snapshot_is_compacted_with_no_room_left_by_what_it_left_out(self, live, tmp_path):
    from services.db.models import Notification
    Notification.insert_many([{"content": "x" * 500}] * 500).execute()
    Database().snapshot(tmp_path / "snapshot.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "snapshot.db")) as copy:
      assert copy.execute("PRAGMA freelist_count").fetchone()[0] == 0

  def test_an_install_replaces_the_configuration_and_starts_the_history_and_the_backup_rows_empty(self, live, tmp_path, application):
    from services.db.models import Notification, Task
    Database().snapshot(tmp_path / "snapshot.db")
    generation = Instance.get().state_generation
    other = Worker.create(hostname="w2", subdomain="blue")
    Container.create(application=application, worker=other)
    Notification.create(content="Added w2.")
    Database().install(tmp_path / "snapshot.db", {})
    assert [worker.hostname for worker in Worker.select()] == ["w1"] and [c.id for c in Container.select()] == [live.id]
    assert [model.select().count() for model in (ContainerEvent, WorkerEvent, Notification, Task, Backup)] == [0] * 5
    assert Instance.get().state_generation != generation
    assert [path for path in config.DB_PATH.parent.iterdir() if ".restore-" in path.name] == []

  def test_a_backup_that_holds_history_is_installed_without_it(self, live, tmp_path):
    from services.db.models import Notification
    with contextlib.closing(sqlite3.connect(tmp_path / "old.db")) as copy:
      database.connection().backup(copy)
    Database().install(tmp_path / "old.db", {})
    assert (ContainerEvent.select().count(), Notification.select().count(), Backup.select().count()) == (0, 0, 0)

  def test_an_older_backup_is_migrated_as_a_run_of_its_own(self, live, tmp_path, monkeypatch):
    Project.update(description="Shop").execute()
    Database().snapshot(tmp_path / "snapshot.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "snapshot.db")) as copy:
      copy.execute("ALTER TABLE project RENAME COLUMN description TO summary")
      copy.commit()
    monkeypatch.setattr(migrations, "STEPS", [
        *migrations.STEPS, lambda db: db.execute_sql("ALTER TABLE project RENAME COLUMN summary TO description")])

    run = Database().install(tmp_path / "snapshot.db", {})

    assert (Project.get().description, version(database)) == ("Shop", len(migrations.STEPS))
    assert {key: MigrationRuns().latest()[key] for key in ("run", "trigger")} == {"run": run, "trigger": "restore"}
    assert list(config.DB_PATH.parent.glob("*.migration-*")) == []

  def test_a_backup_from_a_later_sage_is_refused_and_the_live_database_put_back(self, live, tmp_path):
    Database().snapshot(tmp_path / "snapshot.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "snapshot.db")) as copy:
      copy.execute(f"PRAGMA user_version = {len(migrations.STEPS) + 1}")
      copy.commit()
    Worker.create(hostname="w2", subdomain="blue")

    with pytest.raises(SchemaRefused, match="written by a later Sage"):
      Database().install(tmp_path / "snapshot.db", {})

    assert sorted(worker.hostname for worker in Worker.select()) == ["w1", "w2"]
    assert version(database) == len(migrations.STEPS)

  @pytest.mark.parametrize("saved", [True, False], ids=["the backup has the section", "the backup lacks it"])
  def test_a_kept_section_replaces_the_backups(self, live, tmp_path, saved):
    Setting.create(key="s3", value={"bucket": "old"})
    Database().snapshot(tmp_path / "snapshot.db")
    if not saved:
      Setting.delete().execute()
      Database().snapshot(tmp_path / "snapshot.db")
    Database().install(tmp_path / "snapshot.db", {"s3": {"bucket": "current"}})
    assert Setting.get(Setting.key == "s3").value == {"bucket": "current"}

  def test_an_install_stopped_right_after_the_copy_leaves_a_complete_restore(self, live, tmp_path, monkeypatch):
    from services.db.models import Notification
    Setting.create(key="s3", value={"bucket": "old"})
    Database().snapshot(tmp_path / "snapshot.db")
    generation = Instance.get().state_generation
    Worker.create(hostname="w2", subdomain="blue")
    Setting.update(value={"bucket": "current"}).execute()

    class Killed(BaseException):
      pass

    def kill(*_):
      raise Killed()

    monkeypatch.setattr(Database(), "migrate", kill)
    with pytest.raises(Killed):
      Database().install(tmp_path / "snapshot.db", {"s3": {"bucket": "current"}})
    assert [worker.hostname for worker in Worker.select()] == ["w1"]
    assert Instance.get().state_generation != generation
    assert Setting.get(Setting.key == "s3").value == {"bucket": "current"}
    assert [model.select().count() for model in (ContainerEvent, WorkerEvent, Notification, Backup)] == [0] * 4

  @pytest.mark.parametrize("damage", ["not a database", "a damaged page"])
  def test_a_backup_that_is_not_a_sound_database_is_refused_and_nothing_changes(self, live, tmp_path, damage):
    path = tmp_path / "broken.db"
    if damage == "not a database":
      path.write_bytes(b"not a database at all" * 100)
    else:
      Database().snapshot(path)
      data = bytearray(path.read_bytes())
      page = next(offset for offset in range(4096, len(data), 4096) if data[offset] == 0x0D)
      data[page + 8:page + 64] = bytes(range(56))
      path.write_bytes(bytes(data))
    before = counts(database)
    with pytest.raises(Exception):
      Database().install(path, {})
    assert counts(database) == before

  def test_safety_copies_go_once_past_their_retention_and_nothing_else_does(self, db, monkeypatch):
    import os
    import time
    monkeypatch.setattr(config, "RETENTION_DAYS", 14)
    monkeypatch.setattr(config, "MIGRATION_COPY_RETENTION_DAYS", 90)
    folder, name = config.DB_PATH.parent, config.DB_PATH.name
    ages = {folder / f"{name}.restore-20261001_000000": 15, folder / f"{name}.restore-20261001_000000-journal": 15,
            folder / f"{name}.restore-20261110_000000": 13, folder / "notes.restore-20261001_000000": 15,
            folder / f"{name}.migration-0": 91, folder / f"{name}.migration-1": 89, folder / "notes.migration-0": 91}
    for path, days in ages.items():
      path.write_bytes(b"copy")
      os.utime(path, (time.time() - days * 86400,) * 2)
    Database().remove_safety_copies()
    assert [path.name for path in ages if path.exists()] == [
        f"{name}.restore-20261110_000000", "notes.restore-20261001_000000", f"{name}.migration-1", "notes.migration-0"]

  def test_an_install_that_fails_part_way_puts_the_live_database_back(self, live, tmp_path, monkeypatch):
    Database().snapshot(tmp_path / "snapshot.db")
    Worker.create(hostname="w2", subdomain="blue")
    generation = Instance.get().state_generation

    def refuse(*_):
      raise SchemaRefused(config.DB_PATH, "its tables differ")

    monkeypatch.setattr(Database(), "migrate", refuse)
    with pytest.raises(SchemaRefused):
      Database().install(tmp_path / "snapshot.db", {})
    assert sorted(worker.hostname for worker in Worker.select()) == ["w1", "w2"]
    assert (Instance.get().state_generation, ContainerEvent.select().count()) == (generation, 1)
    assert [path for path in config.DB_PATH.parent.iterdir() if ".restore-" in path.name] == []


def test_a_checkpoint_leaves_every_committed_change_in_the_file_alone_and_the_wal_empty(db, tmp_path):
  Project.create(name="demo", label="Demo")
  wal = config.DB_PATH.with_name(f"{config.DB_PATH.name}-wal")
  assert wal.stat().st_size > 0

  Database().checkpoint()

  assert wal.stat().st_size == 0
  copy = tmp_path / "copy.db"
  copy.write_bytes(config.DB_PATH.read_bytes())
  with contextlib.closing(sqlite3.connect(copy)) as file:
    assert file.execute("SELECT name FROM project").fetchall() == [("demo",)]


def test_a_checkpoint_another_connection_holds_up_is_logged_and_does_not_fail(db, caplog):
  Project.create(name="demo", label="Demo")
  db.execute_sql("PRAGMA busy_timeout = 50")
  with contextlib.closing(sqlite3.connect(config.DB_PATH)) as reader:
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM project").fetchone()
    with caplog.at_level(logging.WARNING, logger="services.db.database"):
      Database().checkpoint()
  assert "could not be emptied" in caplog.text
