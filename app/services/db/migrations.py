"""`data.db`'s migrations (`migrator.migrate`): its baseline and its steps."""
import logging

from peewee import SqliteDatabase

import config
from utils.common import get_env, new_id

from . import migrator

logger = logging.getLogger(__name__)

# 0.1.3's `data.db`, as a fresh install of it creates it.
BASELINE = """
CREATE TABLE "application" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "project_id" VARCHAR(255) NOT NULL, "name" VARCHAR(255) NOT NULL, "label" VARCHAR(255) NOT NULL, "description" VARCHAR(255), "type" VARCHAR(255) NOT NULL, "image" VARCHAR(255), "repo" VARCHAR(255), "path" VARCHAR(255) NOT NULL, "env" TEXT, "args" TEXT, "build_secrets" TEXT, "command" VARCHAR(255), "status" VARCHAR(255) NOT NULL, "domains_synced" INTEGER NOT NULL, "container_count" INTEGER NOT NULL, "deployed_at" DATETIME, "deployed_hash" VARCHAR(255), FOREIGN KEY ("project_id") REFERENCES "project" ("name") ON DELETE RESTRICT);
CREATE TABLE "backup" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "type" VARCHAR(255) NOT NULL, "s3_path" VARCHAR(255) NOT NULL, "source_volume_name" VARCHAR(255), "application_id" INTEGER, FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE CASCADE);
CREATE TABLE "container" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "application_id" INTEGER NOT NULL, "worker_id" VARCHAR(255) NOT NULL, "status" VARCHAR(255) NOT NULL, "domain_tag" VARCHAR(255), FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE RESTRICT, FOREIGN KEY ("worker_id") REFERENCES "worker" ("hostname"));
CREATE TABLE "domain" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "application_id" INTEGER NOT NULL, "name" VARCHAR(255) NOT NULL, "type" VARCHAR(255) NOT NULL, "port" INTEGER NOT NULL, FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE CASCADE);
CREATE TABLE "event" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "container_id" INTEGER NOT NULL, "type" VARCHAR(255) NOT NULL, "application_task_id" VARCHAR(255) NOT NULL, "container_task_id" VARCHAR(255) NOT NULL, FOREIGN KEY ("container_id") REFERENCES "container" ("id") ON DELETE CASCADE);
CREATE TABLE "instance" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL);
CREATE TABLE "notification" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "type" VARCHAR(255) NOT NULL, "content" VARCHAR(255) NOT NULL, "link" VARCHAR(255));
CREATE TABLE "project" ("name" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "label" VARCHAR(255) NOT NULL, "description" VARCHAR(255), "env" TEXT, "application_count" INTEGER NOT NULL);
CREATE TABLE "setting" ("key" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "value" TEXT);
CREATE TABLE "task" ("task_id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "name" VARCHAR(255) NOT NULL, "scopes" TEXT, "params" TEXT, "executor" VARCHAR(255) NOT NULL, "status" VARCHAR(255) NOT NULL, "started_at" DATETIME, "finished_at" DATETIME);
CREATE TABLE "volume" ("id" INTEGER NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "name" VARCHAR(255) NOT NULL, "path" VARCHAR(255) NOT NULL, "backup_cron" VARCHAR(255), "application_id" INTEGER NOT NULL, FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE CASCADE);
CREATE TABLE "worker" ("hostname" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "ip" CHAR(15) NOT NULL);
CREATE INDEX "application_project_id" ON "application" ("project_id");
CREATE UNIQUE INDEX "application_project_id_name" ON "application" ("project_id", "name");
CREATE INDEX "backup_application_id" ON "backup" ("application_id");
CREATE INDEX "container_application_id" ON "container" ("application_id");
CREATE UNIQUE INDEX "container_application_id_worker_id" ON "container" ("application_id", "worker_id");
CREATE INDEX "container_worker_id" ON "container" ("worker_id");
CREATE INDEX "domain_application_id" ON "domain" ("application_id");
CREATE UNIQUE INDEX "domain_application_id_name_type" ON "domain" ("application_id", "name", "type");
CREATE INDEX "event_container_id" ON "event" ("container_id");
CREATE UNIQUE INDEX "event_container_id_application_task_id_container_task_id" ON "event" ("container_id", "application_task_id", "container_task_id");
CREATE INDEX "notification_created_at" ON "notification" ("created_at");
CREATE INDEX "task_created_at" ON "task" ("created_at");
CREATE INDEX "task_finished_at" ON "task" ("finished_at");
CREATE INDEX "volume_application_id" ON "volume" ("application_id");
CREATE UNIQUE INDEX "volume_application_id_name" ON "volume" ("application_id", "name");
"""


# 0.2's tables, by name, each with its indexes, as `from_0_1_3` builds them.
SCHEMA_0_2 = {
    "instance": """
CREATE TABLE "instance" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "state_generation" VARCHAR(255) NOT NULL);
""",
    "project": """
CREATE TABLE "project" ("name" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "label" VARCHAR(255) NOT NULL, "description" VARCHAR(255), "env" TEXT, "application_count" INTEGER NOT NULL CHECK (application_count >= 0));
""",
    "application": """
CREATE TABLE "application" ("id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "project_id" VARCHAR(255) NOT NULL, "name" VARCHAR(255) NOT NULL, "label" VARCHAR(255) NOT NULL, "description" VARCHAR(255), "type" VARCHAR(255) NOT NULL CHECK ("type" IN ('docker', 'git')), "image" VARCHAR(255), "repo" VARCHAR(255), "path" VARCHAR(255) NOT NULL, "env" TEXT, "args" TEXT, "build_secrets" TEXT, "command" VARCHAR(255), "container_count" INTEGER NOT NULL CHECK (container_count >= 0), FOREIGN KEY ("project_id") REFERENCES "project" ("name") ON DELETE RESTRICT);
CREATE INDEX "application_project_id" ON "application" ("project_id");
CREATE UNIQUE INDEX "application_project_id_name" ON "application" ("project_id", "name");
""",
    "worker": """
CREATE TABLE "worker" ("hostname" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "subdomain" VARCHAR(63) NOT NULL, "removing" INTEGER NOT NULL);
CREATE UNIQUE INDEX "worker_subdomain" ON "worker" ("subdomain");
""",
    "volume": """
CREATE TABLE "volume" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "name" VARCHAR(255) NOT NULL, "path" VARCHAR(255) NOT NULL, "backup_cron" VARCHAR(255), "application_id" INTEGER NOT NULL, FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE CASCADE);
CREATE INDEX "volume_application_id" ON "volume" ("application_id");
CREATE UNIQUE INDEX "volume_application_id_name" ON "volume" ("application_id", "name");
""",
    "container": """
CREATE TABLE "container" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "application_id" INTEGER NOT NULL, "worker_id" VARCHAR(255) NOT NULL, "intent" VARCHAR(255) CHECK ("intent" IN ('deploy', 'stop', 'delete')), FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE RESTRICT, FOREIGN KEY ("worker_id") REFERENCES "worker" ("hostname"));
CREATE INDEX "container_application_id" ON "container" ("application_id");
CREATE INDEX "container_worker_id" ON "container" ("worker_id");
CREATE UNIQUE INDEX "container_application_id_worker_id" ON "container" ("application_id", "worker_id");
""",
    "domain": """
CREATE TABLE "domain" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "name" VARCHAR(63) NOT NULL, "type" VARCHAR(255) NOT NULL CHECK ("type" IN ('internal', 'public', 'tcp')), "port" INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535), "container_id" VARCHAR(255), "namespace_worker_id" VARCHAR(255), "service" VARCHAR(63), "pool" VARCHAR(63), "cors_origins" TEXT, FOREIGN KEY ("container_id") REFERENCES "container" ("id") ON DELETE SET NULL, FOREIGN KEY ("namespace_worker_id") REFERENCES "worker" ("hostname") ON DELETE RESTRICT);
CREATE INDEX "domain_container_id" ON "domain" ("container_id");
CREATE INDEX "domain_namespace_worker_id" ON "domain" ("namespace_worker_id");
CREATE UNIQUE INDEX "domain_name_namespace_worker" ON "domain" ("name", coalesce(namespace_worker_id, ''));
""",
    "backup": """
CREATE TABLE "backup" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "type" VARCHAR(255) NOT NULL CHECK ("type" IN ('platform', 'application')), "s3_path" VARCHAR(255) NOT NULL, "storage_id" VARCHAR(255) NOT NULL, "source_volume_id" VARCHAR(255), "source_volume_name" VARCHAR(255), "container_id" VARCHAR(255), "worker_hostname" VARCHAR(255), "application_id" INTEGER, "slot_at" DATETIME, "automatic" INTEGER NOT NULL, "status" VARCHAR(255) NOT NULL CHECK ("status" IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')), "reason" VARCHAR(255), "deleted_at" DATETIME, FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE CASCADE);
CREATE INDEX "backup_application_id" ON "backup" ("application_id");
""",
    "notification": """
CREATE TABLE "notification" ("id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "type" VARCHAR(255) NOT NULL CHECK ("type" IN ('info', 'success', 'warning', 'error')), "content" VARCHAR(255) NOT NULL, "link" VARCHAR(255));
CREATE INDEX "notification_created_at" ON "notification" ("created_at");
""",
    "task": """
CREATE TABLE "task" ("task_id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "name" VARCHAR(255) NOT NULL, "scopes" TEXT, "params" TEXT, "labels" TEXT, "executor" VARCHAR(255) NOT NULL, "status" VARCHAR(255) NOT NULL CHECK ("status" IN ('completed', 'failed', 'cancelled')), "started_at" DATETIME, "finished_at" DATETIME);
CREATE INDEX "task_created_at" ON "task" ("created_at");
CREATE INDEX "task_finished_at" ON "task" ("finished_at");
""",
    "container_event": """
CREATE TABLE "container_event" ("id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "subject_id" VARCHAR(255) NOT NULL, "operation_id" VARCHAR(255), "transition" VARCHAR(255) NOT NULL CHECK ("transition" IN ('admitted', 'rejected', 'succeeded', 'failed', 'interrupted', 'recovered', 'recovery_failed', 'verified', 'observed')), "reason" TEXT, "task_id" VARCHAR(255), "dedupe_key" VARCHAR(255), "container_id" VARCHAR(255), "action" VARCHAR(255) NOT NULL CHECK ("action" IN ('deploy', 'stop', 'delete', 'backup_stopped', 'backup_preserve', 'restore_stopped', 'reset')), "application_name" VARCHAR(255) NOT NULL, "worker_hostname" VARCHAR(255) NOT NULL, FOREIGN KEY ("container_id") REFERENCES "container" ("id") ON DELETE SET NULL);
CREATE INDEX "containerevent_operation_id" ON "container_event" ("operation_id");
CREATE UNIQUE INDEX "containerevent_dedupe_key" ON "container_event" ("dedupe_key");
CREATE INDEX "containerevent_container_id" ON "container_event" ("container_id");
CREATE INDEX "containerevent_created_at" ON "container_event" ("created_at");
""",
    "worker_event": """
CREATE TABLE "worker_event" ("id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "subject_id" VARCHAR(255) NOT NULL, "operation_id" VARCHAR(255), "transition" VARCHAR(255) NOT NULL CHECK ("transition" IN ('admitted', 'rejected', 'succeeded', 'failed', 'interrupted', 'recovered', 'recovery_failed', 'verified', 'observed')), "reason" TEXT, "task_id" VARCHAR(255), "dedupe_key" VARCHAR(255), "worker_id" VARCHAR(255), "action" VARCHAR(255) NOT NULL CHECK ("action" IN ('add', 'remove', 'configure', 'infrastructure', 'repair', 'reboot', 'connectivity', 'readiness', 'ownership')), FOREIGN KEY ("worker_id") REFERENCES "worker" ("hostname") ON DELETE SET NULL);
CREATE INDEX "workerevent_operation_id" ON "worker_event" ("operation_id");
CREATE UNIQUE INDEX "workerevent_dedupe_key" ON "worker_event" ("dedupe_key");
CREATE INDEX "workerevent_worker_id" ON "worker_event" ("worker_id");
CREATE INDEX "workerevent_created_at" ON "worker_event" ("created_at");
""",
}


def rows(db: SqliteDatabase, table: str) -> list[dict]:
  cursor = db.execute_sql(f'SELECT * FROM "{table}"')
  return [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]


def rebuild_table(db: SqliteDatabase, table: str, schema: str, copied: list[dict]):
  """Replaces `table` with the one `schema` creates, holding the `copied` rows."""
  # SQLite's procedure: the new table under another name, the old one dropped, the new one renamed into its place,
  # so the other tables' foreign keys keep naming it.
  create, *indexes = migrator.statements(schema)
  db.execute_sql(create.replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{table}_rebuilt"', 1))
  for row in copied:
    db.execute_sql(f'INSERT INTO "{table}_rebuilt" ({", ".join(f'"{key}"' for key in row)}) '
                   f'VALUES ({", ".join("?" * len(row))})', list(row.values()))
  db.execute_sql(f'DROP TABLE IF EXISTS "{table}"')
  db.execute_sql(f'ALTER TABLE "{table}_rebuilt" RENAME TO "{table}"')
  for index in indexes:
    db.execute_sql(index)


def from_0_1_3(db: SqliteDatabase) -> list[dict]:
  """0.1.3's database to 0.2's. The configuration comes across with containers and volumes under new opaque ids and
  every worker under a random subdomain; the Domains, the history (events, notifications, tasks) and the backup
  catalog start empty. 0.1.3's runtimes and volume data stay where 0.1.3 left them on the workers, so every container
  reads not deployed until it is deployed again: an install that passes this step, on its way to any later version,
  has every application stopped. Its actions, in order: on each worker, stop 0.1.3's runtimes, move each volume's data
  to its new place and remove 0.1.3's folders; in Cloudflare and the store, what 0.1.3 left; in Sage, the Domains to
  add again."""
  def rebuild(table: str, copied: list[dict]):
    rebuild_table(db, table, SCHEMA_0_2[table], copied)

  instances, projects, applications = rows(db, "instance"), rows(db, "project"), rows(db, "application")
  workers, volumes, containers, domains = rows(db, "worker"), rows(db, "volume"), rows(db, "container"), rows(db, "domain")
  # 0.1.3 kept each application's runtime and volumes on a worker under its qualified name.
  qualified = {row["id"]: f"{row['project_id']}-{row['name']}" for row in applications}
  stamp = ("created_at", "updated_at")
  new_volumes = [{**{key: row[key] for key in (*stamp, "name", "path", "backup_cron", "application_id")}, "id": new_id()}
                 for row in volumes]
  new_containers = [{**{key: row[key] for key in (*stamp, "application_id", "worker_id")}, "id": new_id(),
                     "intent": None} for row in containers]
  new_workers = [{**{key: row[key] for key in ("hostname", *stamp)}, "subdomain": new_id(), "removing": 0}
                 for row in workers]
  rebuild("instance", [{**{key: row[key] for key in ("id", *stamp)}, "state_generation": new_id()} for row in instances])
  rebuild("project", projects)
  rebuild("application", [{key: row[key] for key in (
      "id", *stamp, "project_id", "name", "label", "description", "type", "image", "repo", "path", "env", "args",
      "build_secrets", "command", "container_count")} for row in applications])
  rebuild("worker", new_workers)
  rebuild("volume", new_volumes)
  rebuild("container", new_containers)
  logger.info(f"Kept [{len(instances)}] instance, [{len(projects)}] projects and [{len(applications)}] applications "
              "unchanged, with their settings and ids.")
  for row in new_workers:
    logger.info(f"Worker [{row['hostname']}] got the random subdomain [{row['subdomain']}]: this version gives each "
                "worker a subdomain of its own for its internal domains. Rename it on the worker's page if you like.")
  for old, row in zip(volumes, new_volumes):
    logger.info(f"Volume [{row['name']}] of [{qualified[row['application_id']]}] now has the id [{row['id']}] (was "
                f"[{old['id']}]): this version keeps a volume's data on the worker under its id.")
  for old, row in zip(containers, new_containers):
    logger.info(f"Container of [{qualified[row['application_id']]}] on [{row['worker_id']}] now has the id [{row['id']}] "
                f"(was [{old['id']}]).")
  if containers:
    logger.warning(f"Every container ([{len(containers)}]) now reads not deployed: this version runs containers from "
                   "new places on the workers and can't take over the ones 0.1.3 started. Deploy each again once the "
                   "steps at the end of this log are done.")
  dropped = {
      "domain": ("domains", "this version routes domains differently and can't convert 0.1.3's. Each is listed at the "
                 "end of this log, to add again"),
      "backup": ("backup list entries", "the list is rebuilt from the S3 store; 0.1.3's backups are not in it (see the "
                 "end of this log)"),
      "notification": ("notifications", "the history starts empty in this version"),
      "task": ("task records", "the history starts empty in this version"),
      "container_event": ("container events", "the history starts empty in this version"),
      "worker_event": ("worker events", "the history starts empty in this version"),
  }
  for table, (rows_name, why) in dropped.items():
    if table in db.get_tables() and (count := db.execute_sql(f'SELECT count(*) FROM "{table}"').fetchone()[0]):
      logger.info(f"Removed [{count}] {rows_name}: {why}.")
    rebuild(table, [])
  if count := db.execute_sql('SELECT count(*) FROM "event"').fetchone()[0]:
    logger.info(f"Removed [{count}] events: the history starts empty in this version.")
  db.execute_sql('DROP TABLE "event"')

  home, domain = config.WORKER_HOME_DIR, get_env("DOMAIN")
  actions = []
  for hostname in sorted({row["worker_id"] for row in new_containers}):
    placed = sorted((row for row in new_containers if row["worker_id"] == hostname),
                    key=lambda row: qualified[row["application_id"]])
    for container in placed:
      old = f"{home}/applications/{qualified[container['application_id']]}"
      actions.append({"where": f"Worker [{hostname}]", "command": f'[ ! -f {old}/docker-compose.yml ] || docker compose '
                      f'-f {old}/docker-compose.yml down',
                      "why": f"stop [{qualified[container['application_id']]}], still running as 0.1.3 started it: "
                      "this version doesn't manage 0.1.3's containers."})
    for container in placed:
      old = f"{home}/applications/{qualified[container['application_id']]}"
      for volume in sorted((row for row in new_volumes if row["application_id"] == container["application_id"]),
                           key=lambda row: row["name"]):
        new = f"{home}/containers/{container['id']}/volumes/{volume['id']}"
        actions.append({"where": f"Worker [{hostname}]", "command": f"if [ -e {new} ]; then echo '{new} exists already'; "
                        f"else mkdir -p {new.rsplit('/', 1)[0]} && mv {old}/volumes/{volume['name']} {new}; fi",
                        "why": f"move the data of [{qualified[container['application_id']]}]'s volume [{volume['name']}] "
                        f"to where this version keeps it. Do it before deploying [{qualified[container['application_id']]}] "
                        "again: without it, the container starts with an empty volume."})
    actions.append({"where": f"Worker [{hostname}]", "command": f"rm -rf {home}/applications && rm -f " + " ".join(
        f"{home}/revisions/{qualified[row['application_id']]}" for row in placed),
        "why": "delete what 0.1.3 left of its applications on this worker. Run it only once every move above has "
        "succeeded: it deletes any volume data still in the old place."})
  actions.append({"where": "Cloudflare", "command": None,
                  "why": f"Delete the DNS record [*.{domain}] and the tunnel [{domain}-sage], which 0.1.3 created. "
                  "This version gives each public domain its own record on its own tunnel; until these are deleted, a "
                  "name without its own record goes to the idle old tunnel and gets Cloudflare's error page."})
  actions.append({"where": "S3 store", "command": None,
                  "why": "0.1.3's backups (under [backups/platform/] and [backups/applications/], named without an id) "
                         "can't be used by this version: it names backups differently, so it doesn't list or restore "
                         "them, and neither the retention period nor deleting a volume removes them. They stay in the "
                         "store until you delete them. Once every application runs again, take new backups on the "
                         "Backups page, then delete the old ones from the store."})
  for row in sorted(domains, key=lambda row: (qualified[row["application_id"]], row["type"], row["name"])):
    actions.append({"where": "Sage", "command": None,
                    "why": f"The {row['type']} domain [{row['name']}] (port [{row['port']}]) of "
                    f"[{qualified[row['application_id']]}] was removed by this upgrade. Add it again on the card of "
                    f"the [{qualified[row['application_id']]}] container that should serve it, on its application's "
                    "page."})
  return actions


def from_0_2_0(db: SqliteDatabase) -> list[dict]:
  """0.2.0's database: every application gets its response headers' changes, none yet, so each sends Sage's
  defaults. A domain belongs to its container and is deleted with it, so the domains 0.2.0 kept unlinked after their
  container's deletion are removed: they have no DNS record, and a route one left on a worker goes with the Domain
  pass, as any Domain gone. Its actions list them, to add again. An internal or TCP domain gets the applications
  allowed to call it, none yet."""
  db.execute_sql('ALTER TABLE "application" ADD COLUMN "response_headers" TEXT')
  logger.info("Applications get their response headers: none is changed, so every application sends the defaults.")
  domains = rows(db, "domain")
  rebuild_table(db, "domain", """
CREATE TABLE "domain" ("id" VARCHAR(255) NOT NULL PRIMARY KEY, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL, "name" VARCHAR(63) NOT NULL, "type" VARCHAR(255) NOT NULL CHECK ("type" IN ('internal', 'public', 'tcp')), "port" INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535), "container_id" VARCHAR(255) NOT NULL, "namespace_worker_id" VARCHAR(255), "service" VARCHAR(63), "pool" VARCHAR(63), "cors_origins" TEXT, FOREIGN KEY ("container_id") REFERENCES "container" ("id") ON DELETE CASCADE, FOREIGN KEY ("namespace_worker_id") REFERENCES "worker" ("hostname") ON DELETE RESTRICT);
CREATE INDEX "domain_container_id" ON "domain" ("container_id");
CREATE INDEX "domain_namespace_worker_id" ON "domain" ("namespace_worker_id");
CREATE UNIQUE INDEX "domain_name_namespace_worker" ON "domain" ("name", coalesce(namespace_worker_id, ''));
""", [row for row in domains if row["container_id"] is not None])
  for statement in (
      'CREATE TABLE "domaincaller" ("id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "created_at" DATETIME NOT NULL, '
      '"updated_at" DATETIME NOT NULL, "domain_id" VARCHAR(255) NOT NULL, "application_id" INTEGER NOT NULL, '
      'FOREIGN KEY ("domain_id") REFERENCES "domain" ("id") ON DELETE CASCADE, '
      'FOREIGN KEY ("application_id") REFERENCES "application" ("id") ON DELETE CASCADE)',
      'CREATE INDEX "domaincaller_domain_id" ON "domaincaller" ("domain_id")',
      'CREATE INDEX "domaincaller_application_id" ON "domaincaller" ("application_id")',
      'CREATE UNIQUE INDEX "domaincaller_domain_id_application_id" ON "domaincaller" ("domain_id", "application_id")',
  ):
    db.execute_sql(statement)
  logger.info("Internal and TCP domains get the applications allowed to call them: none yet.")
  unlinked = sorted((row for row in domains if row["container_id"] is None), key=lambda row: (row["type"], row["name"]))
  actions = []
  for row in unlinked:
    where = f" on worker [{row['namespace_worker_id']}]" if row["namespace_worker_id"] else ""
    logger.info(f"Removed the unlinked {row['type']} domain [{row['name']}]{where} ([{row['id']}]): this version "
                "deletes a domain with its container.")
    actions.append({"where": "Sage", "command": None,
                    "why": f"The unlinked {row['type']} domain [{row['name']}]{where} (port [{row['port']}]) was "
                    "removed by this upgrade: a domain now belongs to its container. Add it again on the card of the "
                    "container that should serve it, on its application's page."})
  return actions


STEPS = [from_0_1_3, from_0_2_0]
