import logging
from fastapi import APIRouter
from datetime import datetime

from services.manager import Manager
from services.settings import Settings
from services.tailscale import Tailscale
from utils.common import get_env

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


@router.get("/summary")
async def get_summary():
  return Manager().get_system_summary()
