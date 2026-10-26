import logging

from fastapi import APIRouter

from services.manager import Manager
from services.tailscale import Tailscale
from utils.common import get_env, utcnow
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

router = APIRouter()
start_time = utcnow()


def settings_status():
  try:
    return Manager().settings_status()
  except Exception as e:
    logger.error(f"Failed to build the settings status: {e}")
    return None


@router.get("")
def get():
  return {
      "version": Manager().version,
      "latest_version": Manager().latest_version,
      "domain": get_env("DOMAIN"),
      "name": get_env("TS_NAME"),
      "ip": Tailscale().ip(),
      "start_time": start_time.isoformat(),
      "state": Manager().task_state(),
      "restoring_from": Manager().restoring_from(),
      "workers": Manager().worker_summary,
      "settings": settings_status(),
  }


@router.get("/release")
async def get_release():
  return Manager().get_latest_release()


@router.post("/release/refresh")
async def refresh_release():
  Manager().add_task(
      task=Manager().refresh_latest_release,
      scopes={"common"},
      executor="common",
      on_conflict=OnConflict.REPLACE,
  )
  return {"message": "Release refresh queued."}


@router.get("/summary")
async def get_summary():
  return Manager().get_system_summary()
