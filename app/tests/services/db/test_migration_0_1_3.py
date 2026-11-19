import contextlib
import json
import logging
import os
import re
import sqlite3
import subprocess

import pytest

import config
from services.db import encryption, migrations
from services.db.database import Database
from services.db.models import (Application, Backup, Container, ContainerEvent, Domain, Instance, Notification, Project, Setting, Task, Volume,
                                Worker, WorkerEvent)
from services.db.models import db as database
from utils.common import OPAQUE_ID

T = "2026-01-01 00:00:00"

ROWS = [
    f"INSERT INTO instance VALUES ('aaaaaaaaaaaaaaaa', '{T}', '{T}')",
    f"INSERT INTO setting VALUES ('s3', '{T}', '{T}', :setting)",
    f"INSERT INTO project VALUES ('shop', '{T}', '{T}', 'Shop', NULL, NULL, 2)",
    "INSERT INTO application (id, created_at, updated_at, project_id, name, label, description, type, image, repo, path, "
    "env, args, build_secrets, command, status, domains_synced, container_count, deployed_at, deployed_hash) VALUES "
    f"(3, '{T}', '{T}', 'shop', 'web', 'Web', NULL, 'docker', 'nginx', NULL, 'Dockerfile', :env, NULL, NULL, NULL, "
    f"'active', 1, 2, '{T}', 'h1')",
    "INSERT INTO application (id, created_at, updated_at, project_id, name, label, description, type, image, repo, path, "
    "env, args, build_secrets, command, status, domains_synced, container_count, deployed_at, deployed_hash) VALUES "
    f"(7, '{T}', '{T}', 'shop', 'api', 'API', 'The API', 'git', NULL, 'https://git.test/api', 'Dockerfile', NULL, NULL, "
    "NULL, 'serve', 'inactive', 0, 0, NULL, NULL)",
    f"INSERT INTO worker VALUES ('w1', '{T}', '{T}', '100.64.0.1')",
    f"INSERT INTO worker VALUES ('w2', '{T}', '{T}', '100.64.0.2')",
    f"INSERT INTO volume VALUES (1, '{T}', '{T}', 'data', '/data', '0 3 * * *', 3)",
    f"INSERT INTO volume VALUES (2, '{T}', '{T}', 'cache', '/cache', NULL, 3)",
    f"INSERT INTO container VALUES (10, '{T}', '{T}', 3, 'w1', 'active', NULL)",
    f"INSERT INTO container VALUES (11, '{T}', '{T}', 3, 'w2', 'error', 'eu')",
    f"INSERT INTO domain VALUES (1, '{T}', '{T}', 3, 'shop', 'public', 80)",
    f"INSERT INTO domain VALUES (2, '{T}', '{T}', 3, 'web', 'internal', 8080)",
    f"INSERT INTO event VALUES (1, '{T}', '{T}', 10, 'deploy', 'a', 'b')",
    f"INSERT INTO notification VALUES (1, '{T}', '{T}', 'success', 'Deployed.', NULL)",
    f"INSERT INTO task VALUES ('t1', '{T}', '{T}', 'deploy', NULL, NULL, 'app', 'completed', NULL, '{T}')",
    f"INSERT INTO backup VALUES (1, '{T}', '{T}', 'platform', 'base/backups/platform/20260101_000000.db.enc', NULL, NULL)",
]


@pytest.fixture
def migrated(data_dir, monkeypatch, caplog):
  """A 0.1.3 database with rows in every table, after this version's boot, logged at INFO as production is."""
  caplog.set_level(logging.INFO)
  monkeypatch.setattr(encryption, "_cipher", None)
  encryption.initialize_encryption(os.environ["ENCRYPTION_KEY"])
  with contextlib.closing(sqlite3.connect(config.DB_PATH)) as file:
    file.executescript(migrations.BASELINE)
    for row in ROWS:
      file.execute(row, {"env": encryption.encrypt("A=1\nB=2"), "setting": encryption.encrypt('{"bucket": "b"}')})
    file.commit()
  Database()
  yield database
  database.close()


def test_it_ends_at_this_versions_schema(migrated):
  assert migrated.execute_sql("PRAGMA user_version").fetchone()[0] == len(migrations.STEPS)


def test_the_instance_keeps_its_id_and_gets_a_generation(migrated):
  (instance,) = Instance.select()
  assert instance.id == "aaaaaaaaaaaaaaaa"
  assert re.fullmatch(OPAQUE_ID, instance.state_generation)


def test_settings_and_projects_come_through(migrated):
  assert Setting.get(Setting.key == "s3").value == {"bucket": "b"}
  assert [(project.name, project.label, project.application_count) for project in Project.select()] == [("shop", "Shop", 2)]


def test_applications_keep_their_ids_and_values_and_new_ones_follow_the_highest(migrated):
  web, api = Application.get_by_id(3), Application.get_by_id(7)
  assert (web.name, web.type, web.image, web.env, web.container_count) == ("web", "docker", "nginx", "A=1\nB=2", 2)
  assert (api.type, api.repo, api.description, api.command) == ("git", "https://git.test/api", "The API", "serve")
  assert web.response_headers is None and api.response_headers is None
  assert Application.create(project="shop", name="new", label="New", image="nginx").id == 8


def test_workers_keep_their_hostnames_under_a_random_subdomain_each(migrated):
  workers = list(Worker.select().order_by(Worker.hostname))
  assert [(worker.hostname, worker.removing) for worker in workers] == [("w1", False), ("w2", False)]
  assert all(re.fullmatch(OPAQUE_ID, worker.subdomain) for worker in workers)
  assert workers[0].subdomain != workers[1].subdomain


def test_volumes_and_containers_get_opaque_ids_and_keep_their_links(migrated):
  volumes = {volume.name: volume for volume in Volume.select()}
  assert {name: (volume.application_id, volume.path, volume.backup_cron) for name, volume in volumes.items()} == {
      "data": (3, "/data", "0 3 * * *"), "cache": (3, "/cache", None)}
  containers = {container.worker_id: container for container in Container.select()}
  assert {hostname: (container.application_id, container.intent) for hostname, container in containers.items()} == {
      "w1": (3, None), "w2": (3, None)}
  assert all(re.fullmatch(OPAQUE_ID, item.id) for item in (*volumes.values(), *containers.values()))


@pytest.mark.parametrize("model", [Domain, Backup, Notification, Task, ContainerEvent, WorkerEvent],
                         ids=lambda model: model.__name__)
def test_domains_history_and_the_backup_catalog_start_empty(migrated, model):
  assert model.select().count() == 0


def test_the_old_event_table_is_gone(migrated):
  assert "event" not in migrated.get_tables()


def test_a_failure_part_way_leaves_the_0_1_3_database_as_it_was(data_dir, monkeypatch):
  monkeypatch.setattr(encryption, "_cipher", None)
  encryption.initialize_encryption(os.environ["ENCRYPTION_KEY"])
  with contextlib.closing(sqlite3.connect(config.DB_PATH)) as file:
    file.executescript(migrations.BASELINE)
    for row in ROWS:
      file.execute(row, {"env": None, "setting": None})
    file.execute("UPDATE application SET container_count = -1 WHERE id = 7")
    file.commit()
    before = file.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()

  with pytest.raises(Exception, match="its migration step 1 failed .*CHECK constraint failed"):
    Database()
  database.close()

  with contextlib.closing(sqlite3.connect(config.DB_PATH)) as file:
    assert file.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == before
    assert file.execute("SELECT count(*) FROM domain").fetchone()[0] == 2
    assert file.execute("PRAGMA user_version").fetchone()[0] == 0


def actions() -> list[dict]:
  """The run's manual actions as its log ends with them: where, the command (between `###` lines) and why."""
  from services.db.migration_log import migration_runs
  [run] = migration_runs()
  found = []
  for line in run["lines"]:
    if line["level"] == "CRITICAL":
      text, _, command = line["message"].partition("\n###\n")
      where, _, why = text.partition(" - ")
      found.append({"where": where, "command": command.removesuffix("\n###") or None,
                    "why": why.removeprefix("Run command to ")})
  return found


def test_the_actions_go_worker_by_worker_then_cloudflare_the_store_and_the_domains(migrated):
  web = {container.worker_id: container.id for container in Container.select()}
  volumes = {volume.name: volume.id for volume in Volume.select()}
  steps = [(action["where"], action["command"] or "", action["why"]) for action in actions()]

  assert [where for where, _, _ in steps] == ["Worker [w1]"] * 4 + ["Worker [w2]"] * 4 + ["Cloudflare", "S3 store"] + ["Sage"] * 2
  assert "docker compose -f /opt/sage/applications/shop-web/docker-compose.yml down" in steps[0][1]
  assert (f"mv /opt/sage/applications/shop-web/volumes/cache /opt/sage/containers/{web['w1']}/volumes/{volumes['cache']}"
          in steps[1][1])
  assert f"/opt/sage/containers/{web['w1']}/volumes/{volumes['data']}" in steps[2][1]
  assert steps[3][1] == "rm -rf /opt/sage/applications && rm -f /opt/sage/revisions/shop-web"
  assert f"/opt/sage/containers/{web['w2']}/volumes/{volumes['data']}" in steps[6][1]
  assert steps[0][2] == "stop [shop-web], still running as 0.1.3 started it: this version doesn't manage 0.1.3's containers."
  assert steps[8][2].startswith("Delete the DNS record [*.example.test] and the tunnel [example.test-sage], which 0.1.3 created.")
  assert "neither the retention period nor deleting a volume removes them" in steps[9][2]
  assert [why for where, _, why in steps if where == "Sage"] == [
      "The internal domain [web] (port [8080]) of [shop-web] was removed by this upgrade. Add it again on the Domains "
      "page, linked to the [shop-web] container that should serve it.",
      "The public domain [shop] (port [80]) of [shop-web] was removed by this upgrade. Add it again on the Domains "
      "page, linked to the [shop-web] container that should serve it."]


def test_a_move_puts_the_data_in_its_new_place_and_never_over_data_already_there(migrated, tmp_path, monkeypatch):
  [move] = [action["command"] for action in actions()
            if "/volumes/data " in (action["command"] or "") and action["where"] == "Worker [w2]"]
  old = tmp_path / "applications/shop-web/volumes/data"
  old.mkdir(parents=True)
  (old / "db.sqlite").write_text("kept")
  command = move.replace("/opt/sage", str(tmp_path))
  new = tmp_path / command.split(" mv ")[1].split(" ")[1].rstrip(";")

  subprocess.run(["sh", "-c", command], check=True)
  assert ((new / "db.sqlite").read_text(), old.exists()) == ("kept", False)

  old.mkdir(parents=True)
  (old / "db.sqlite").write_text("second")
  result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
  assert (result.stdout.strip(), (new / "db.sqlite").read_text(), (old / "db.sqlite").read_text()) == (
      f"{new} exists already", "kept", "second")


def test_its_log_says_what_it_did_with_the_old_and_new_ids_then_what_to_do_by_hand(migrated):
  from services.db.migration_log import migration_runs
  [run] = migration_runs()
  web = {container.worker_id: container.id for container in Container.select()}
  lines = [(line["level"], line["message"]) for line in run["lines"]]
  messages = [message for _, message in lines]
  assert "Kept [1] instance, [1] projects and [2] applications unchanged, with their settings and ids." in messages
  assert f"Container of [shop-web] on [w1] now has the id [{web['w1']}] (was [10])." in messages
  assert ("WARNING", "Every container ([2]) now reads not deployed: this version runs containers from new places on the "
          "workers and can't take over the ones 0.1.3 started. Deploy each again once the steps at the end of this log "
          "are done.") in lines
  assert any(message.startswith("Removed [2] domains: ") for message in messages)
  assert "Removed [1] events: the history starts empty in this version." in messages
  assert any(message.startswith("Migrated [") and f"from version [0] to [{len(migrations.STEPS)}]" in message
             for message in messages)
  assert "Applications get their response headers: none is changed, so every application sends the defaults." in messages
  actions = [(level, message) for level, message in lines if level == "CRITICAL"]
  assert actions[0] == ("CRITICAL", "Worker [w1] - Run command to stop [shop-web], still running as 0.1.3 started it: "
                                    "this version doesn't manage 0.1.3's containers.\n###\n"
                                    "[ ! -f /opt/sage/applications/shop-web/docker-compose.yml ] || docker compose -f "
                                    "/opt/sage/applications/shop-web/docker-compose.yml down\n###")
  assert actions[-1][1].startswith("Sage - The public domain [shop] (port [80]) of [shop-web] was removed by this upgrade.")
  assert lines[-len(actions):] == actions
