import logging

import pytest

import config
from services.db import BaseModel, Database, Instance, Project
from services.db import db as database
from services.db.database import MODELS


def columns(db, table):
  return {column.name for column in db.get_columns(table)}


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

  def test_a_not_null_column_without_a_default_stops_the_startup(self, db):
    db.execute_sql("ALTER TABLE project DROP COLUMN label")
    with pytest.raises(ValueError, match="not null but has no default"):
      Database().reconcile_schema()
