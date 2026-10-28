import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException

from services.db import Notification

logger = logging.getLogger(__name__)

router = APIRouter()


def list_notifications(limit: int = None, from_ts: datetime = None, to_ts: datetime = None):
  """Notifications ordered by creation date (latest first)."""
  query = Notification.select().order_by(Notification.created_at.desc())

  if from_ts:
    query = query.where(Notification.created_at > from_ts)
  if to_ts:
    query = query.where(Notification.created_at <= to_ts)
  if limit:
    query = query.limit(limit)

  return list(query.dicts())


@router.get("/")
async def get_notifications(limit: int = None, from_ts: datetime = None, to_ts: datetime = None):
  try:
    return list_notifications(limit, from_ts, to_ts)
  except Exception as e:
    logger.error(f"Failed to fetch notifications: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to list Notification with error: {e}")
