import logging
import os

from fastapi import APIRouter

from services.tailscale import Tailscale
from utils.common import get_env

logger = logging.getLogger(__name__)

with open("/app/VERSION") as f:
    version = f.read().strip()

router = APIRouter()


@router.get("")
async def get():
    return {
        "org": get_env("ORG"),
        "version": version,
        "domain": get_env("DOMAIN"),
        "hostname": get_env("HOSTNAME"),
        "ip": Tailscale().ip(),
    }
