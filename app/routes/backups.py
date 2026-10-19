import logging
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from services.db import Backup
from services.manager import Manager
from utils.api import generic_delete, generic_get, generic_list, get_request_models
from utils.queue import OnConflict

logger = logging.getLogger(__name__)

router = APIRouter()

# How long a download link works. S3 checks it when a request starts, so a download that
# has begun is not cut off when it passes.
DOWNLOAD_LINK_SECONDS = 60


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
  """Get current platform backup and discovery status."""
  return {
      "platform_backup_in_progress": Manager().is_task_running(Manager().backup_database_s3),
      # Queued counts too: discovery waits behind running app work.
      "discovery_in_progress": Manager().has_task(Manager().discover_s3_backups),
  }


def require_s3():
  """Every backup lives in S3, which is optional."""
  if not Manager().s3.s3_config:
    raise HTTPException(status_code=400, detail="S3 is not configured. Set it up in Settings first.")


@router.post("/")
def create_backup(request: Request):
  """Trigger backup - platform backup by default, or volume backup if nested under volume."""
  require_s3()
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


@router.post("/discover")
def discover_backups(request: Request):
  """List the platform backups and volume archives in S3 that aren't listed yet."""
  require_s3()
  Manager().add_task(
      task=Manager().discover_s3_backups,
      scopes={"platform", "app"},
      executor="platform",
      task_id=request.state.task_id,
  )
  return {"status": "OK"}


def download_filename(backup: Backup) -> str:
  """A name for the saved file: a volume backup's path says which volume it is from."""
  parts = backup.s3_path.strip("/").split("/")
  name = "-".join(parts[-5:]) if backup.type == "application" else parts[-1]
  return re.sub(r"[^A-Za-z0-9._-]", "_", name)


@router.post("/{backup}/download", dependencies=[Depends(inject_backup)])
def download_backup(request: Request):
  """A short-lived link that downloads the backup file straight from S3. The link is made
  when asked for and not stored; the file is encrypted with ENCRYPTION_KEY either way."""
  require_s3()
  backup = request.state.models["backup"]

  if not Manager().s3.key_exists(backup.s3_path, raw_path=True):
    raise HTTPException(status_code=404, detail="The backup file is no longer in S3.")

  url = Manager().s3.create_presigned_download_url(
      backup.s3_path, expires_in=DOWNLOAD_LINK_SECONDS, raw_path=True,
      filename=download_filename(backup))
  logger.info(f"Download link for backup {backup.id} ({backup.s3_path}) valid for {DOWNLOAD_LINK_SECONDS}s.")
  return {"url": url, "expires_in": DOWNLOAD_LINK_SECONDS}


@router.delete("/{backup}", dependencies=[Depends(inject_backup)])
def delete_backup(request: Request):
  """Delete backup - only platform backups for now."""
  require_s3()
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
  require_s3()
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

  # Blocks Sage (restoring state) until the restore has finished.
  if not Manager().add_task(
      task=Manager().restore_database_from_s3,
      scopes={"platform", "app"},
      params={"s3_path": backup.s3_path},
      executor="platform",
      task_id=request.state.task_id,
      restore=True,
  ):
    raise HTTPException(status_code=409, detail="A platform operation is already in progress.")

  return {"status": "OK"}
