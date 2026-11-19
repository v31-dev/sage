import contextlib
import logging
import os
import sqlite3

import pytest

import config
from services.db import encryption, migrations, migrator
from services.db.database import Database
from services.db.migration_log import migration_runs
from services.db.models import Container, Domain
from services.db.models import db as database

T = "2026-01-01 00:00:00"

ROWS = [
    f"INSERT INTO instance VALUES ('aaaaaaaaaaaaaaaa', '{T}', '{T}', 'bbbbbbbbbbbbbbbb')",
    f"INSERT INTO project VALUES ('shop', '{T}', '{T}', 'Shop', NULL, NULL, 1)",
    "INSERT INTO application (id, created_at, updated_at, project_id, name, label, description, type, image, repo, path, "
    f"env, args, build_secrets, command, container_count) VALUES (3, '{T}', '{T}', 'shop', 'web', 'Web', NULL, "
    "'docker', 'nginx', NULL, 'Dockerfile', NULL, NULL, NULL, NULL, 1)",
    f"INSERT INTO worker VALUES ('w1', '{T}', '{T}', 'red', 0)",
    f"INSERT INTO container VALUES ('c1', '{T}', '{T}', 3, 'w1', NULL)",
    f"INSERT INTO domain VALUES ('d1', '{T}', '{T}', 'shop', 'public', 80, 'c1', NULL, 'shop', 'eu', "
    "'[\"https://a.test\"]')",
    f"INSERT INTO domain VALUES ('d2', '{T}', '{T}', 'api', 'internal', 8080, 'c1', 'w1', NULL, NULL, NULL)",
    f"INSERT INTO domain VALUES ('d3', '{T}', '{T}', 'old', 'public', 80, NULL, NULL, NULL, NULL, NULL)",
    f"INSERT INTO domain VALUES ('d4', '{T}', '{T}', 'legacy', 'tcp', 5432, NULL, 'w1', NULL, NULL, NULL)",
]


@pytest.fixture
def migrated(data_dir, monkeypatch, caplog):
  """A 0.2.0 database with linked and unlinked domains, after this version's boot."""
  caplog.set_level(logging.INFO)
  monkeypatch.setattr(encryption, "_cipher", None)
  encryption.initialize_encryption(os.environ["ENCRYPTION_KEY"])
  setting = next(statement for statement in migrator.statements(migrations.BASELINE)
                 if statement.startswith('CREATE TABLE "setting"'))
  with contextlib.closing(sqlite3.connect(config.DB_PATH)) as file:
    file.executescript("".join(migrations.SCHEMA_0_2.values()) + setting + ";\nPRAGMA user_version = 1;")
    for row in ROWS:
      file.execute(row)
    file.commit()
  Database()
  yield database
  database.close()


def test_linked_domains_come_through_unchanged(migrated):
  assert [(domain.id, domain.name, domain.type, domain.port, domain.container_id, domain.namespace_worker_id,
           domain.service, domain.pool, domain.cors_origins) for domain in Domain.select().order_by(Domain.id)] == [
      ("d1", "shop", "public", 80, "c1", None, "shop", "eu", ["https://a.test"]),
      ("d2", "api", "internal", 8080, "c1", "w1", None, None, None)]


def test_unlinked_domains_are_removed_and_listed_to_add_again(migrated):
  [run] = migration_runs()
  lines = [(line["level"], line["message"]) for line in run["lines"]]
  assert ("INFO", "Removed the unlinked public domain [old] ([d3]): this version deletes a domain with its "
                  "container.") in lines
  assert [message for level, message in lines if level == "CRITICAL"] == [
      "Sage - The unlinked public domain [old] (port [80]) was removed by this upgrade: a domain now belongs to its "
      "container. Add it again on the card of the container that should serve it, on its application's page.",
      "Sage - The unlinked tcp domain [legacy] on worker [w1] (port [5432]) was removed by this upgrade: a domain now "
      "belongs to its container. Add it again on the card of the container that should serve it, on its "
      "application's page."]


def test_a_container_deleted_afterwards_takes_its_domains(migrated):
  Container.get_by_id("c1").delete_instance()
  assert Domain.select().count() == 0
