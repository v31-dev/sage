"""Each application's lines, as Vector sends them from its containers: a shard for each application, named by its
qualified name (`logs/<project>-<app>.db`), with a full-text index of the messages."""
import re
from datetime import datetime

from peewee import CharField, Model, SqliteDatabase, TextField
from playhouse.sqlite_ext import FTS5Model, RowIDField, SearchField

import config
from services.db import Application, Project
from services.updates import UpdateFeed, Updates
from utils.common import utcnow

from .store import Store, contains, newest, stored_time

# The stream of the lines Sage writes into an application's logs: what it did to a container.
MARKER_STREAM = "sage"


def application_schema(db: SqliteDatabase) -> Store:
  # Named as the shards' tables and indexes are, which take their names from the models'.
  class ContainerLogs(Model):
    hostname = CharField()
    ts = CharField()
    stream = CharField()
    message = TextField()

    class Meta:
      database = db
      indexes = ((("ts",), False),)

  class ContainerLogsIndex(FTS5Model):
    rowid = RowIDField()
    message = SearchField()

    class Meta:
      database = db
      options = {"content": ContainerLogs, "content_rowid": "id", "tokenize": "trigram"}

  db.create_tables([ContainerLogs, ContainerLogsIndex], safe=True)
  # The index follows the lines as they are written and deleted.
  db.execute_sql(
      """
      CREATE TRIGGER IF NOT EXISTS containerlogs_ai
      AFTER INSERT ON containerlogs BEGIN
        INSERT INTO containerlogsindex(rowid, message) VALUES (new.id, new.message);
      END
      """
  )
  db.execute_sql(
      """
      CREATE TRIGGER IF NOT EXISTS containerlogs_ad
      AFTER DELETE ON containerlogs BEGIN
        INSERT INTO containerlogsindex(containerlogsindex, rowid, message) VALUES('delete', old.id, old.message);
      END
      """
  )
  return Store(db, ContainerLogs, ContainerLogsIndex)


class ApplicationsMixin:
  def _shard(self, name: str) -> str:
    """`name` as an application's shard; refused if it could leave the logs directory or is another kind of log's
    file."""
    if not re.fullmatch(r"[a-z0-9-]+", name or "") or name in self.FILES:
      raise ValueError(f"Invalid application name: {name!r}")
    return name

  def _application_names(self) -> set[str]:
    return {application.qualified_name for application in Application.select(Application, Project).join(Project)}

  def application_store(self, name: str) -> Store:
    return self._store(self._shard(name))

  def write_application_lines(self, name: str, rows: list):
    """An application's lines, to its shard and its feed."""
    stored = self._insert(self._shard(name), rows)
    Updates().publish(UpdateFeed.APPLICATION_LOGS, stored, key=name)

  def write_marker(self, name: str, hostname: str, text: str):
    """What Sage did to the application's container on `hostname`: a line of its own, at the manager's time."""
    self.write_application_lines(
        name, [{"hostname": hostname, "ts": stored_time(utcnow()), "stream": MARKER_STREAM, "message": text}])

  def query_application_lines(self, name: str, hostname: str = "", search: str = "", from_ts: datetime | None = None,
                              to_ts: datetime | None = None, after_id: int = 0, limit: int = config.LIST_MAX_ROWS) -> dict:
    """An application's lines, in the order they were stored: the newest `limit` (after the line `after_id`, if
    given) from `hostname` (all workers if empty) that contain `search` anywhere, in any case, and whose
    time is at or after `from_ts` and before `to_ts`, and whether there were more. The search walks the message index
    newest first (the trigram tokenizer finds three characters or more through it; shorter text, or a shard indexed
    before it, is scanned) and stops at the cap."""
    store = self.application_store(name)
    lines, index = store.lines, store.index
    query = lines.select()
    order = lines.id.desc()
    if search:
      query = query.join(index, on=(lines.id == index.rowid)).where(contains(index.message, search))
      order = index.rowid.desc()
    if hostname:
      query = query.where(lines.hostname == hostname)
    if from_ts:
      query = query.where(lines.ts >= stored_time(from_ts))
    if to_ts:
      query = query.where(lines.ts < stored_time(to_ts))
    if after_id:
      query = query.where(lines.id > after_id)
    rows, more = newest(query, order, limit)
    return {"lines": rows[::-1], "more": more}
