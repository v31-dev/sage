import logging
from fastapi import APIRouter

from utils.common import get_env
from services.tailscale import Tailscale


logger = logging.getLogger(__name__)


router = APIRouter()

@router.get("")
async def get():
  return {
    "org": get_env("ORG"),
    "version": 0.1,
    "domain": get_env("DOMAIN"),
    "hostname": get_env("HOSTNAME"),
    "ip": Tailscale().ip(),
  }