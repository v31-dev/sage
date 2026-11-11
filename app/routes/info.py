from fastapi import APIRouter

from services.manager import Manager
from services.releases import Releases
from utils.logging import NO_ACCESS_LOG

router = APIRouter()


@router.get("", openapi_extra=NO_ACCESS_LOG)  # the healthcheck, and each page's app info
def get():
  return Manager().info()


@router.get("/release")
def get_release():
  return Releases().latest()


@router.post("/release/refresh")
def refresh_release():
  return {"task_id": Manager().request_release_check(quiet=False)}


@router.get("/summary")
def get_summary():
  return Manager().get_system_summary()
