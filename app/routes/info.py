import logging
import os

from fastapi import APIRouter

from services.settings import Settings
from services.tailscale import Tailscale
from utils.common import get_env

logger = logging.getLogger(__name__)

with open("/app/VERSION") as f:
  version = f.read().strip()

router = APIRouter()


@router.get("")
async def get():
  return {
      "version": version,
      "domain": Settings().get("cloudflare", "domain"),
      "hostname": get_env("HOSTNAME"),
      "ip": Tailscale().ip(),
  }
