from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Query

import config
from routes.containers import router as containers_router
from services.applications import commands
from services.applications.lookup import find_application
from services.logs import Logs
from services.manager import Manager
from services.metrics.series import PERIODS
from utils.logging import NO_ACCESS_LOG
from values.names import qualified_name

router = APIRouter()


@router.post("/")
def create_application(project: str, data: dict = Body(...)):
  application = commands.create_application(project, data.get("label"), data.get("description"))
  return Manager().application_page(project, application.name)


@router.get("/{application}")
def get_application(project: str, application: str):
  return Manager().application_page(project, application)


@router.get("/{application}/logs", openapi_extra=NO_ACCESS_LOG)  # what the logs view loads
def get_application_logs(project: str, application: str, hostname: str = "", search: str = "",
                         from_ts: datetime | None = None, to_ts: datetime | None = None, after_id: int = 0,
                         limit: Annotated[int, Query(ge=1, le=config.LIST_MAX_ROWS)] = config.LIST_MAX_ROWS):
  """The application's lines from its containers: the newest `limit` that match (after the line `after_id`, if
  given), and whether there were more."""
  find_application(project, application)
  return Logs().query_application_lines(qualified_name(project, application), hostname=hostname, search=search,
                                        from_ts=from_ts, to_ts=to_ts, after_id=after_id, limit=limit)


@router.put("/{application}")
def update_application(project: str, application: str, data: dict = Body(...)):
  commands.patch_application(find_application(project, application).id, data)
  return Manager().application_page(project, application)


@router.delete("/{application}", status_code=204)
def delete_application(project: str, application: str):
  commands.delete_application(find_application(project, application).id)


@router.post("/{application}/deploy")
def deploy_all(project: str, application: str):
  return {"task_ids": Manager().request_deploy_all(find_application(project, application).id)}


@router.post("/{application}/stop")
def stop_all(project: str, application: str):
  return {"task_ids": Manager().request_stop_all(find_application(project, application).id)}


@router.get("/{application}/metrics")
def get_metrics(project: str, application: str, period: Literal[tuple(PERIODS)] = "1h"):
  return Manager().application_graphs(project, application, period)


router.include_router(containers_router, prefix="/{application}/containers")
# TODO(36 step 5B-2): router.include_router(volumes_router, prefix="/{application}/volumes")
