import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException

from services.db import Notification

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def get_notifications(limit: int = None, from_ts: datetime = None, to_ts: datetime = None):
  """
  Get notifications ordered by creation date (latest first).
  """
  try:
    query = Notification.select().order_by(Notification.created_at.desc())

    if from_ts:
      query = query.where(Notification.created_at > from_ts)
    if to_ts:
      query = query.where(Notification.created_at <= to_ts)
    if limit:
      query = query.limit(limit)

    # Query notifications ordered by created_at descending, skip and limit
    return list(query.dicts())
  except Exception as e:
    logger.error(f"Failed to fetch notifications: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to list Notification with error: {e}")
