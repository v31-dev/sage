import logging
import os
from fastapi import APIRouter
from datetime import datetime

from services.manager import Manager
from services.settings import Settings
from services.tailscale import Tailscale
from utils.common import get_env
from utils.logging import run_in_executor_with_context

logger = logging.getLogger(__name__)

with open("/app/VERSION") as f:
  version = f.read().strip()

router = APIRouter()
start_time = datetime.now()


@router.get("")
async def get():
  return {
      "version": version,
      "domain": Settings().get("cloudflare", "domain"),
      "hostname": get_env("HOSTNAME"),
      "ip": Tailscale().ip(),
      "start_time": start_time.isoformat(),
  }


@router.get("/summary")
async def get_summary():
  return Manager().get_system_summary()


@router.post("/restart")
async def restart():
  """
  Trigger a restart of the full compose stack via the Docker API socket.
  Progress is not tracked — if the restart fails the operator must intervene.
  """
  run_in_executor_with_context(Manager().restart, all=True)
  return {"message": "Restart initiated."}
