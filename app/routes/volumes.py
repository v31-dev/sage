from croniter import croniter
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from routes.backups import router as backups_router
from services.db import Volume
from utils.api import (
    get_request_models,
    generic_create,
    generic_delete,
    generic_get,
    generic_list,
    generic_update,
    parse_api_data,
)


def inject_volume(request: Request):
  models = get_request_models(request, ["project", "application"])
  application = models["application"]

  # Fetch and store volume
  volume_name = request.path_params.get("volume")

  models["volume"] = generic_get(
      Volume,
      (Volume.application == application) & (Volume.name == volume_name),
      return_model=True,
  )

  return None


router = APIRouter()


def validate_backup_cron(expression: str | None):
  if expression == "":
    expression = None

  if expression is None:
    return None

  value = expression.strip()
  if value == "":
    return None

  parts = value.split()
  if len(parts) != 5:
    raise HTTPException(status_code=400, detail="backup_cron must be a 5-part cron expression.")

  if not croniter.is_valid(value):
    raise HTTPException(status_code=400, detail="Invalid backup_cron expression.")

  return expression


@router.get("/")
def list_volumes(request: Request):
  return generic_list(Volume, (Volume.application == request.state.models["application"]))


@router.post("/")
def create_volume(request: Request, volume_data: dict = Body(...)):
  data = {
      "application": request.state.models["application"],
      "name": volume_data.get("name"),
      "path": volume_data.get("path"),
      "backup_cron": validate_backup_cron(volume_data.get("backup_cron")),
  }

  return generic_create(Volume, data)


@router.put("/{volume}", dependencies=[Depends(inject_volume)])
def update_volume(request: Request, volume_data: dict = Body(...)):
  volume = request.state.models["volume"]
  data = parse_api_data(volume_data, ["name", "path", "backup_cron"])

  if "backup_cron" in data:
    data["backup_cron"] = validate_backup_cron(data["backup_cron"])

  return generic_update(Volume, volume, data)


@router.delete("/{volume}", dependencies=[Depends(inject_volume)])
def delete_volume(request: Request):
  return generic_delete(Volume, request.state.models["volume"])


router.include_router(
    backups_router,
    prefix="/{volume}/backups",
    dependencies=[Depends(inject_volume)],
)
