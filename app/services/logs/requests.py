"""Traefik's requests on every worker: its JSON access lines, parsed into columns in `logs/requests.db`."""
import json
from datetime import datetime

from peewee import CharField, FloatField, IntegerField, Model, SqliteDatabase, TextField

from services.updates import UpdateFeed, Updates

from .store import Store, contains, newest, stored_time

REQUESTS = "requests"
# The container whose access lines are the requests.
TRAEFIK = "traefik"


def requests_schema(db: SqliteDatabase) -> Store:
  class Requests(Model):
    ts = CharField()
    worker = CharField()
    host = CharField()
    method = CharField()
    path = TextField()
    status = IntegerField()
    duration_ms = FloatField()
    router = CharField()
    service = CharField()
    client = CharField()
    size = IntegerField()

    class Meta:
      database = db
      indexes = ((("ts",), False),)

  db.create_tables([Requests], safe=True)
  return Store(db, Requests)


def access_line(hostname: str, message: str) -> dict | None:
  """Traefik's access line (JSON) as a request; None for one of its own lines."""
  try:
    line = json.loads(message)
  except ValueError:
    return None
  if not isinstance(line, dict) or "RequestMethod" not in line:
    return None
  return {
      "ts": stored_time(line.get("StartUTC", "")),
      "worker": hostname,
      "host": line.get("RequestHost", ""),
      "method": line["RequestMethod"],
      "path": line.get("RequestPath", ""),
      "status": int(line.get("DownstreamStatus") or 0),
      # A Go duration, in nanoseconds.
      "duration_ms": (line.get("Duration") or 0) / 1e6,
      "router": line.get("RouterName", ""),
      "service": line.get("ServiceName", ""),
      "client": line.get("ClientHost", ""),
      "size": int(line.get("DownstreamContentSize") or 0),
  }


class RequestsMixin:
  def write_requests(self, rows: list):
    stored = self._insert(REQUESTS, rows)
    Updates().publish(UpdateFeed.REQUESTS, stored)

  def query_requests(self, worker: str = "", host: str = "", method: str = "", status_class: int | None = None,
                     path: str = "", min_duration_ms: float | None = None, from_ts: datetime | None = None,
                     to_ts: datetime | None = None, after_id: int = 0) -> dict:
    """The newest LOG_QUERY_MAX_ROWS requests (after the request `after_id`, if given), newest first, that match:
    `worker` and `method` exactly, `host` and `path` anywhere in any case, `status_class` the first digit of the status
    (4 for 4xx), `min_duration_ms` or slower, and a start at or after `from_ts` and before `to_ts`; and whether there
    were more."""
    requests = self._store(REQUESTS).lines
    query = requests.select()
    if worker:
      query = query.where(requests.worker == worker)
    if host:
      query = query.where(contains(requests.host, host))
    if method:
      query = query.where(requests.method == method)
    if status_class is not None:
      query = query.where((requests.status >= status_class * 100) & (requests.status < (status_class + 1) * 100))
    if path:
      query = query.where(contains(requests.path, path))
    if min_duration_ms is not None:
      query = query.where(requests.duration_ms >= min_duration_ms)
    if from_ts:
      query = query.where(requests.ts >= stored_time(from_ts))
    if to_ts:
      query = query.where(requests.ts < stored_time(to_ts))
    if after_id:
      query = query.where(requests.id > after_id)
    rows, more = newest(query, requests.id.desc())
    return {"requests": rows, "more": more}
