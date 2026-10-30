from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from services.logs import Logs

router = APIRouter()


@router.get("/")
def list_requests(worker: str = "", host: str = "", method: str = "",
                  status: Literal["", "2xx", "3xx", "4xx", "5xx"] = "", path: str = "",
                  min_duration_ms: Annotated[float | None, Query(ge=0)] = None,
                  from_ts: datetime | None = None, to_ts: datetime | None = None, after_id: int = 0):
  """Traefik's requests on every worker, newest first: the newest 1000 that match (after the request `after_id`, if
  given), and whether there were more."""
  return Logs().query_requests(worker=worker, host=host, method=method,
                               status_class=int(status[0]) if status else None, path=path,
                               min_duration_ms=min_duration_ms, from_ts=from_ts, to_ts=to_ts,
                               after_id=after_id)
