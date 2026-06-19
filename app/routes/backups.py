import logging
from fastapi import APIRouter, Body, Request, Depends, HTTPException

from services.db import Backup
from services.manager import Manager
from utils.api import get_request_models, generic_delete, generic_get, generic_list
from utils.queue import OnConflict

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
  return {"platform_backup_in_progress": Manager().is_task_running("backup_database_s3")}


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

    if not Manager().add_task(
        task=Manager().backup_application_s3,
        scopes={f"app:{application.qualified_name}"},
        params={"application_id": application.id, "volume_ids": [volume.id]},
        executor="app",
        task_id=request.state.task_id,
    ):
      raise HTTPException(status_code=409, detail="Application already has an operation in progress.")
  else:
    # REPLACE: never rejected. Supersedes a pending backup (latest wins) and waits
    # behind a running one, so the user's trigger captures changes made during the
    # in-flight backup, with at most one backup queued behind it.
    Manager().add_task(
        task=Manager().backup_database_s3,
        scopes={"platform", "app"},
        executor="platform",
        task_id=request.state.task_id,
        on_conflict=OnConflict.REPLACE,
    )

  return {"status": "OK"}


@router.delete("/{backup}", dependencies=[Depends(inject_backup)])
def delete_backup(request: Request):
  """Delete backup - only platform backups for now."""
  backup = request.state.models["backup"]

  # Delete backup record from database
  generic_delete(Backup, backup)

  # Delete backup file from S3 asynchronously
  Manager().add_task(
      task=Manager().delete_backup_s3,
      scopes={"common"},
      params={"s3_path": backup.s3_path},
      executor="common",
      task_id=request.state.task_id,
      on_conflict=OnConflict.QUEUE,
  )

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

    if not Manager().add_task(
        task=Manager().restore_application_volume_from_s3,
        scopes={f"app:{application.qualified_name}"},
        params={
            "application_id": application.id,
            "volume_id": volume.id,
            "backup_id": backup.id,
            "target_worker_hostname": target_worker.strip(),
        },
        executor="app",
        task_id=request.state.task_id,
    ):
      raise HTTPException(status_code=409, detail="Application already has an operation in progress.")

    return {"status": "OK"}

  if backup.type != "platform":
    raise HTTPException(status_code=400, detail="Only platform backups can be restored")

  if not Manager().add_task(
      task=Manager().restore_database_from_s3,
      scopes={"platform", "app"},
      params={"s3_path": backup.s3_path},
      executor="platform",
      task_id=request.state.task_id,
  ):
    raise HTTPException(status_code=409, detail="A platform operation is already in progress.")

  return {"status": "OK"}
