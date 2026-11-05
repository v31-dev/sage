import contextlib
import logging

import pytest
from peewee import IntegrityError

import config
from services.db import Application, Backup, BaseModel, Container, Database, Domain, Event, Instance, Project, SchemaRefused, Volume, Worker
from services.db import db as database
from services.db.database import MODELS

ORPHAN_DOMAIN = [
    "PRAGMA foreign_keys=OFF",
    "INSERT INTO domain (container_id, name, type, port, status, created_at, updated_at) "
    "VALUES (999, 'ghost', 'public', 80, 'pending', '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
    "PRAGMA foreign_keys=ON",
]


def columns(db, table):
  return {column.name for column in db.get_columns(table)}


def counts(db):
  return {table: db.execute_sql(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in db.get_tables()}


def point_a_domain_at_a_missing_container(db):
  for statement in ORPHAN_DOMAIN:
    db.execute_sql(statement)


@pytest.fixture
def clash(db, worker):
  """A database from before the name index: one worker holds an internal and a TCP name that are the same."""
  db.execute_sql("DROP INDEX domain_name_worker")
  Domain.insert(name="shop", type="internal", port=80, worker=worker).execute()
  Domain.insert(name="shop", type="tcp", port=80, worker=worker).execute()


def test_opens_in_the_data_dir(db, data_dir):
  assert (data_dir / "data.db").exists()


def test_creates_the_data_dir_when_it_is_missing(data_dir, monkeypatch):
  monkeypatch.setattr(config, "DB_PATH", data_dir / "not" / "yet" / "data.db")
  Database()
  database.close()
  assert config.DB_PATH.exists()


def test_every_model_is_registered_and_has_a_table(db):
  assert set(MODELS) == set(BaseModel.__subclasses__())
  assert set(db.get_tables()) == {model._meta.table_name for model in MODELS}


@pytest.mark.parametrize("pragma, expected", [
    ("foreign_keys", 1),
    ("journal_mode", "wal"),
    ("busy_timeout", 5000),
    ("cache_size", -64000),
])
def test_applies_the_connection_pragmas(db, pragma, expected):
  assert db.execute_sql(f"PRAGMA {pragma}").fetchone()[0] == expected


class TestInstance:
  def test_a_new_database_has_one_instance_with_an_id(self, db):
    (instance,) = Instance.select()
    assert len(instance.id) == 16

  def test_the_instance_survives_reconciling_and_a_restart(self, db):
    before = Instance.get().id
    Database().reconcile_schema()
    Database()
    assert [instance.id for instance in Instance.select()] == [before]

  def test_a_snapshot_from_before_the_instance_existed_gets_one_when_it_is_restored(self, db):
    db.execute_sql("DROP TABLE instance")
    Database().reconcile_schema()
    assert Instance.select().count() == 1


class TestReconcileSchema:
  @pytest.mark.parametrize("column, expected", [("application_count", 0), ("description", None)])
  def test_adds_a_missing_column_and_keeps_the_rows(self, db, column, expected):
    Project.create(name="demo", label="Demo")
    db.execute_sql(f"ALTER TABLE project DROP COLUMN {column}")
    assert column not in columns(db, "project")

    Database().reconcile_schema()

    assert column in columns(db, "project")
    project = Project.get_by_id("demo")
    assert project.label == "Demo"
    assert getattr(project, column) == expected

  @pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
  def test_every_model_gets_its_missing_columns(self, db, model):
    table = model._meta.table_name
    db.execute_sql(f"ALTER TABLE {table} DROP COLUMN updated_at")

    Database().reconcile_schema()

    assert "updated_at" in columns(db, table)

  def test_a_current_schema_is_left_alone(self, db, caplog):
    with caplog.at_level(logging.INFO, logger="services.db.database"):
      Database().reconcile_schema()
    assert "Adding missing column" not in caplog.text
    assert "Dropping column" not in caplog.text

  def test_drops_a_column_no_model_has_any_more_and_keeps_the_rows(self, db):
    Project.create(name="demo", label="Demo")
    db.execute_sql("ALTER TABLE project ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")

    Database().reconcile_schema()

    assert "retired" not in columns(db, "project")
    assert Project.get_by_id("demo").label == "Demo"

  def test_a_not_null_column_without_a_default_that_no_model_has_does_not_stop_a_new_row(self, db):
    """A worker table from before `online` left the models has it NOT NULL, so a new worker could not be inserted."""
    db.execute_sql("DROP TABLE worker")
    db.execute_sql(
        "CREATE TABLE worker (hostname VARCHAR(255) NOT NULL PRIMARY KEY, ip CHAR(15) NOT NULL, online INTEGER NOT NULL, "
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)")

    Database().reconcile_schema()

    assert "online" not in columns(db, "worker")
    Worker.create(hostname="w1", ip="100.64.0.2")
    assert Worker.get_by_id("w1").ip == "100.64.0.2"

  def test_logs_the_column_it_drops(self, db, caplog):
    db.execute_sql("ALTER TABLE project ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
    with caplog.at_level(logging.INFO, logger="services.db.database"):
      Database().reconcile_schema()
    assert "Dropping column project.retired" in caplog.text

  def test_a_not_null_column_without_a_default_stops_the_startup(self, db):
    db.execute_sql("ALTER TABLE project DROP COLUMN label")
    with pytest.raises(ValueError, match="not null but has no default"):
      Database().reconcile_schema()

  def test_a_new_column_with_an_index_gets_both_on_a_table_with_rows(self, db):
    Worker.create(hostname="w1", ip="100.64.0.2", subdomain="aaa111")
    Worker.create(hostname="w2", ip="100.64.0.3", subdomain="bbb222")
    db.execute_sql("DROP INDEX worker_subdomain")
    db.execute_sql("ALTER TABLE worker DROP COLUMN subdomain")

    Database().reconcile_schema()

    assert "subdomain" in columns(db, "worker")
    assert [row[0] for row in db.execute_sql("SELECT sql FROM sqlite_master WHERE name = 'worker_subdomain'")] == [
        'CREATE UNIQUE INDEX "worker_subdomain" ON "worker" ("subdomain")']
    assert Worker.select().count() == 2

  def test_an_existing_domain_table_gets_the_name_index(self, db):
    db.execute_sql("DROP INDEX domain_name_worker")

    Database().reconcile_schema()

    assert "domain_name_worker" in {index.name for index in db.get_indexes("domain")}

  @pytest.mark.parametrize("type", ["public", "internal", "tcp"])
  def test_domain_sync_columns_are_added_pending_and_keep_the_binding(self, db, application, worker, type):
    container = Container.create(application=application, worker=worker, status="active")
    domain = Domain.create(name="shop", type=type, port=80, container=container, service="shop", pool="eu")
    before = counts(db)
    for column in ("status", "sync_error", "synced_hash", "synced_at"):
      db.execute_sql(f"ALTER TABLE domain DROP COLUMN {column}")

    Database().reconcile_schema()

    stored = Domain.get_by_id(domain.id)
    assert (stored.status, stored.sync_error, stored.synced_hash, stored.synced_at) == ("pending", None, None, None)
    assert (stored.name, stored.type, stored.port, stored.container_id, stored.worker_id, stored.service, stored.pool) == (
        "shop", type, 80, container.id, None if type == "public" else worker.hostname, "shop", "eu")
    assert counts(db) == before

  def test_a_domain_keeps_its_error_and_previous_success_when_its_table_is_rebuilt(self, db, application, worker, clock):
    container = Container.create(application=application, worker=worker)
    domain = Domain.create(name="shop", type="public", port=80, container=container,
                           status="error", sync_error="DNS: unavailable", synced_hash="applied", synced_at=clock.current)
    db.execute_sql("ALTER TABLE domain DROP COLUMN updated_at")

    Database().reconcile_schema()

    stored = Domain.get_by_id(domain.id)
    assert (stored.status, stored.sync_error, stored.synced_hash, stored.synced_at) == (
        "error", "DNS: unavailable", "applied", clock.current)
    assert stored.container_id == container.id

  def test_an_index_no_model_declares_is_dropped_and_the_declared_ones_and_sqlites_own_stay(self, db):
    db.execute_sql("CREATE UNIQUE INDEX domain_name_namespace ON domain (name, (type = 'public'))")
    before = {index.name for index in db.get_indexes("worker")}

    Database().reconcile_schema()

    assert "domain_name_namespace" not in {index.name for index in db.get_indexes("domain")}
    assert {index.name for index in db.get_indexes("worker")} == before
    assert "sqlite_autoindex_worker_1" in before

  def test_a_column_in_a_foreign_key_that_no_model_has_is_dropped_and_the_rows_stay(self, db, application, worker):
    container = Container.create(application=application, worker=worker)
    Domain.create(name="shop", type="internal", port=80, container=container)
    db.execute_sql("ALTER TABLE domain ADD COLUMN application_id INTEGER REFERENCES application (id) ON DELETE CASCADE")

    Database().reconcile_schema()

    assert "application_id" not in columns(db, "domain")
    assert Domain.get(Domain.name == "shop").container_id == container.id

  def test_data_that_refuses_an_index_stops_the_startup_saying_what_failed_and_where_the_fix_is(self, db, clash, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "1.2.3")

    with pytest.raises(SchemaRefused) as refused:
      Database().reconcile_schema()

    message = str(refused.value)
    assert f"Sage 1.2.3 can't update the schema of {config.DB_PATH}" in message
    assert "UNIQUE constraint failed: index 'domain_name_worker'" in message
    assert """The refused index: CREATE UNIQUE INDEX IF NOT EXISTS "domain_name_worker" ON "domain" ("name", coalesce(worker_id, '')).""" in message
    assert "The database was left as it was." in message
    assert message.endswith("How to fix it: https://github.com/v31-dev/sage/releases/tag/v1.2.3")
    assert isinstance(refused.value.__cause__, IntegrityError)


class TestANotNullColumnAddedToATableOtherRowsReference:
  """Adding a NOT NULL column rebuilds its table; with foreign keys on, dropping the old table deletes the rows that
  cascade from it or fails on those that restrict it."""

  @pytest.fixture
  def deployed(self, application, worker):
    container = Container.create(application=application, worker=worker)
    Event.create(container=container, type="deploy", application_task_id="t1", container_task_id="t2")
    Domain.create(name="shop", type="internal", port=80, container=container)

  @pytest.fixture
  def undeployed(self, project):
    application = Application.create(project=project, name="api", label="API", image="nginx")
    Volume.create(application=application, name="data", path="/data")
    Backup.create(type="application", s3_path="backups/api", application=application)

  @pytest.mark.parametrize("table, rows", [
      ("project", "deployed"),
      ("application", "undeployed"),
      ("application", "deployed"),
      ("worker", "deployed"),
      ("container", "deployed"),
  ], ids=[
      "project, which an application restricts",
      "application, whose volumes and backups cascade",
      "application, which a container restricts",
      "worker, which a container and a domain restrict",
      "container, whose events cascade and whose domains are unlinked",
  ])
  def test_every_row_stays_and_the_column_is_not_null(self, db, request, table, rows):
    request.getfixturevalue(rows)
    before = counts(db)
    db.execute_sql(f"ALTER TABLE {table} DROP COLUMN updated_at")

    Database().reconcile_schema()

    assert counts(db) == before
    assert next(column for column in db.get_columns(table) if column.name == "updated_at").null is False


class TestARefusedSchemaStep:
  def test_a_table_it_would_create_is_not_there(self, db, clash):
    # Created before the domain index fails: Peewee creates the tables in the order of their foreign keys.
    db.execute_sql("DROP TABLE backup")

    with pytest.raises(SchemaRefused):
      Database().reconcile_schema()

    assert "backup" not in db.get_tables()

  def test_an_index_it_would_create_is_not_there(self, db, clash):
    db.execute_sql("DROP INDEX application_project_id_name")

    with pytest.raises(SchemaRefused):
      Database().reconcile_schema()

    assert "application_project_id_name" not in {index.name for index in db.get_indexes("application")}

  def test_a_column_it_added_before_the_refusal_is_not_there(self, db, application):
    db.execute_sql("ALTER TABLE project DROP COLUMN description")
    point_a_domain_at_a_missing_container(db)

    with pytest.raises(SchemaRefused):
      Database().reconcile_schema()

    assert "description" not in columns(db, "project")

  def test_rows_that_point_at_nothing_are_refused_and_named(self, db, application):
    point_a_domain_at_a_missing_container(db)

    with pytest.raises(SchemaRefused, match=r"FOREIGN KEY check failed: domain row \d+ points at a missing container"):
      Database().reconcile_schema()

  def test_after_the_data_is_fixed_the_next_start_goes_through(self, db, clash):
    with pytest.raises(SchemaRefused):
      Database().reconcile_schema()

    Domain.delete().where(Domain.type == "tcp").execute()
    Database().reconcile_schema()

    assert "domain_name_worker" in {index.name for index in db.get_indexes("domain")}


@pytest.mark.parametrize("setup, error", [
    ([], None),
    (ORPHAN_DOMAIN, SchemaRefused),
    (["ALTER TABLE project DROP COLUMN label"], ValueError),
], ids=["after a success", "after a refusal", "after any other error"])
def test_foreign_keys_are_on_again(db, application, setup, error):
  for statement in setup:
    db.execute_sql(statement)

  with pytest.raises(error) if error else contextlib.nullcontext():
    Database().reconcile_schema()

  assert db.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1


def test_it_refuses_to_run_inside_a_transaction_where_foreign_keys_cannot_be_turned_off(db):
  db.execute_sql("ALTER TABLE project DROP COLUMN description")

  with db.atomic():
    with pytest.raises(RuntimeError, match="must not run inside a transaction"):
      Database().reconcile_schema()

  assert "description" not in columns(db, "project")
  assert db.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1
