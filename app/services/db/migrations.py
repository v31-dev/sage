"""`data.db`'s migrations (`migrator.migrate`): its baseline and its steps."""
from peewee import SqliteDatabase

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


def from_0_1_3(db: SqliteDatabase):
  # TODO(77 step 2): 0.1.3's database to 0.2's.
  raise NotImplementedError("the migration from 0.1.3 is not written yet")


STEPS = [from_0_1_3]
