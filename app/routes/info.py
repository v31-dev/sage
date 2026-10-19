import logging
from datetime import datetime

from fastapi import APIRouter

from services.manager import Manager
from services.tailscale import Tailscale
from utils.common import get_env
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

router = APIRouter()
start_time = datetime.now()


@router.get("")
def get():
  return {
      "version": Manager().version,
      "latest_version": Manager().latest_version,
      "domain": get_env("DOMAIN"),
      "name": get_env("TS_NAME"),
      "worker_tag": Tailscale().worker_tag,
      "ip": Tailscale().ip(),
      "start_time": start_time.isoformat(),
      "state": Manager().task_state(),
      "restoring_from": Manager().restoring_from(),
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
