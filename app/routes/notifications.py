from datetime import datetime

from fastapi import APIRouter

from services.notification import list_notifications

router = APIRouter()


@router.get("/")
def get_notifications(limit: int | None = None, from_ts: datetime | None = None, to_ts: datetime | None = None):
  return list_notifications(limit, from_ts, to_ts)
