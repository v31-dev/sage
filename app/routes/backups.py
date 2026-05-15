import logging
import os
from fastapi import APIRouter, Body, Request, Depends, HTTPException

from services.db import Backup
from services.manager import Manager
from utils.api import get_request_models, generic_delete, generic_get, generic_list

logger = logging.getLogger(__name__)

router = APIRouter()


def inject_backup(request: Request):
  models = get_request_models(request)

  # Fetch and store backup
  backup_id = request.path_params.get("backup")
  query = Backup.id == backup_id

  if "volume" in models:
    query &= (
        (Backup.application == models["application"])
        & (Backup.source_volume_name == models["volume"].name)
    )

  models["backup"] = generic_get(
      Backup,
      query,
      return_model=True,
  )

  return None


@router.get("/")
def list_backups(request: Request):
  """List backups - platform backups by default, or volume backups if nested under volume."""
  models = get_request_models(request)
  if "volume" in models:
    return generic_list(
        Backup,
        (Backup.type == "application")
        & (Backup.application == models["application"])
        & (Backup.source_volume_name == models["volume"].name),
    )
  else:
    return generic_list(Backup, Backup.type == "platform")


@router.post("/platform_backup_status")
def get_platform_backup_status(request: Request):
  """Get current platformbackup status."""
  return {"platform_backup_in_progress": Manager().is_platform_backup_in_progress()}


@router.post("/")
def create_backup(request: Request):
  """Trigger backup - platform backup by default, or volume backup if nested under volume."""
  models = get_request_models(request)
  if "volume" in models:
    application = models["application"]
    volume = models["volume"]

    if application.status != "inactive":
      raise HTTPException(
          status_code=409,
          detail="Manual backups are only allowed when the application is inactive.",
      )

    resource_error = Manager().get_volume_backup_resource_error(application, [volume])
    if resource_error:
      raise HTTPException(status_code=400, detail=resource_error)

    request.app.state.rocketry["backup_application"].run(
        application=application,
        volume_ids=[volume.id],
    )
  else:
    if Manager().is_platform_backup_in_progress():
      raise HTTPException(status_code=409, detail="Platform backup already in progress")

    request.app.state.rocketry["backup_database"].run()

  return {"status": "OK"}


@router.delete("/{backup}", dependencies=[Depends(inject_backup)])
def delete_backup(request: Request):
  """Delete backup - only platform backups for now."""
  backup = request.state.models["backup"]

  # Delete backup record from database
  generic_delete(Backup, backup)

  # Delete backup file from S3 asynchronously
  request.app.state.rocketry["delete_backup_s3"].run(s3_path=backup.s3_path)

  return {"status": "OK"}


@router.post("/{backup}/restore", dependencies=[Depends(inject_backup)])
async def restore_backup(request: Request, restore_data: dict = Body(default={})):
  """Restore backup."""
  models = get_request_models(request, ["backup"])
  backup = models["backup"]

  if "volume" in models:
    application = models["application"]
    volume = models["volume"]
    target_worker = restore_data.get("target_worker") if isinstance(restore_data, dict) else None

    if application.status != "inactive":
      raise HTTPException(
          status_code=409,
          detail="Volume restores are only allowed when the application is inactive.",
      )

    if not isinstance(target_worker, str) or target_worker.strip() == "":
      raise HTTPException(status_code=400, detail="target_worker is required for volume restore")

    try:
      await Manager().restore_application_volume_from_s3(
          application,
          volume,
          backup,
          target_worker.strip(),
      )
      return {"status": "OK"}
    except ValueError as e:
      raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
      raise HTTPException(status_code=500, detail=f"Restore failed: {str(e)}")

  if backup.type != "platform":
    raise HTTPException(status_code=400, detail="Only platform backups can be restored")

  try:
    await Manager().restore_database_from_s3(backup.s3_path)
    return {"status": "OK"}
  except Exception as e:
    raise HTTPException(status_code=500, detail=f"Restore failed: {str(e)}")
