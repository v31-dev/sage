import logging
from datetime import datetime

from fastapi import APIRouter

from services.manager import Manager
from services.settings import Settings
from services.tailscale import Tailscale
from utils.common import get_env
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

router = APIRouter()
start_time = datetime.now()


@router.get("")
async def get():
  return {
      "version": Manager().version,
      "latest_version": Manager().latest_version,
      "domain": Settings().get("cloudflare", "domain"),
      "hostname": get_env("HOSTNAME"),
      "ip": Tailscale().ip(),
      "start_time": start_time.isoformat(),
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
