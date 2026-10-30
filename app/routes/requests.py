from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from services.logs import Logs, timestamp

router = APIRouter()


def at(value: datetime | None) -> str:
  """A time from a query as the request store writes it; UTC if it has no zone."""
  if value is None:
    return ""
  if value.tzinfo is not None:
    value = value.astimezone(timezone.utc)
  return timestamp(f"{value:%Y-%m-%dT%H:%M:%S.%f}Z")


@router.get("/")
def list_requests(worker: str = "", host: str = "", method: str = "",
                  status: Literal["", "2xx", "3xx", "4xx", "5xx"] = "", path: str = "",
                  min_duration_ms: Annotated[float | None, Query(ge=0)] = None,
                  from_ts: datetime | None = None, to_ts: datetime | None = None):
  """Traefik's requests on every worker, newest first: the newest 1000 that match, and whether there were more."""
  return Logs().query_requests(worker=worker, host=host, method=method,
                               status_class=int(status[0]) if status else None, path=path,
                               min_duration_ms=min_duration_ms, from_ts=at(from_ts), to_ts=at(to_ts))
