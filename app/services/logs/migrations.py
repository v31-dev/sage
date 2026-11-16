"""The log files' migrations (`migrator.migrate`): each kind's baseline and steps."""
from peewee import SqliteDatabase

# 0.1.3's application shard (`logs/<project>-<app>.db`), as it created one; the full-text index makes its own tables.
APPLICATION_BASELINE = """
CREATE TABLE "containerlogs" ("id" INTEGER NOT NULL PRIMARY KEY, "hostname" VARCHAR(255) NOT NULL, "ts" VARCHAR(255) NOT NULL, "stream" VARCHAR(255) NOT NULL, "message" TEXT NOT NULL);
CREATE VIRTUAL TABLE "containerlogsindex" USING fts5 ("message", content="containerlogs", content_rowid=id, tokenize="trigram");
CREATE INDEX "containerlogs_ts" ON "containerlogs" ("ts");
CREATE TRIGGER containerlogs_ad
      AFTER DELETE ON containerlogs BEGIN
        INSERT INTO containerlogsindex(containerlogsindex, rowid, message) VALUES('delete', old.id, old.message);
      END;
CREATE TRIGGER containerlogs_ai
      AFTER INSERT ON containerlogs BEGIN
        INSERT INTO containerlogsindex(rowid, message) VALUES (new.id, new.message);
      END;
"""

APPLICATION_STEPS = []


# 0.1.3's `logs/requests.db`, as it created it.
REQUESTS_BASELINE = """
CREATE TABLE "requests" ("id" INTEGER NOT NULL PRIMARY KEY, "ts" VARCHAR(255) NOT NULL, "worker" VARCHAR(255) NOT NULL, "host" VARCHAR(255) NOT NULL, "method" VARCHAR(255) NOT NULL, "path" TEXT NOT NULL, "status" INTEGER NOT NULL, "duration_ms" REAL NOT NULL, "router" VARCHAR(255) NOT NULL, "service" VARCHAR(255) NOT NULL, "client" VARCHAR(255) NOT NULL, "size" INTEGER NOT NULL);
CREATE INDEX "requests_ts" ON "requests" ("ts");
"""

REQUESTS_STEPS = []


# 0.1.3's `logs/tasks.db`, as it created it.
TASKS_BASELINE = """
CREATE TABLE "tasklogs" ("id" INTEGER NOT NULL PRIMARY KEY, "task_id" VARCHAR(255) NOT NULL, "sub_task_id" VARCHAR(255) NOT NULL, "ts" VARCHAR(255) NOT NULL, "level" VARCHAR(255) NOT NULL, "logger" VARCHAR(255) NOT NULL, "message" TEXT NOT NULL);
CREATE INDEX "tasklogs_task_id_sub_task_id" ON "tasklogs" ("task_id", "sub_task_id");
CREATE INDEX "tasklogs_ts" ON "tasklogs" ("ts");
"""


def tasks_from_0_1_3(db: SqliteDatabase):
  """0.1.3's lines also named the part of the task they were logged in."""
  db.execute_sql('DROP INDEX "tasklogs_task_id_sub_task_id"')
  db.execute_sql('ALTER TABLE "tasklogs" DROP COLUMN "sub_task_id"')
  db.execute_sql('CREATE INDEX "tasklogs_task_id" ON "tasklogs" ("task_id")')


TASKS_STEPS = [tasks_from_0_1_3]
