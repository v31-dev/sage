from fastapi import APIRouter

from services.backups.download import download_link
from services.backups.payload import platform_backup, platform_backups
from services.manager import Manager
from services.s3 import S3

router = APIRouter()


@router.get("/")
def list_backups():
  return platform_backups()


@router.get("/platform_backup_status")
def get_status():
  return Manager().backup_status()


@router.post("/")
def back_up():
  """A platform backup into the configured store."""
  S3().session()  # a Conflict without S3, where the request alone skips quietly, as the schedule's does
  return {"task_id": Manager().request_platform_backup()}


@router.post("/discover")
def discover_backups():
  """Lists the backups in the configured store that the table does not hold yet, and drops those gone from it."""
  S3().session()  # a Conflict without S3, where the request alone skips quietly, as the schedule's does
  return {"task_id": Manager().request_backup_discovery()}


@router.get("/{backup}/download")
def download_backup(backup: str):
  return download_link(platform_backup(backup))


@router.delete("/{backup}")
def delete_backup(backup: str):
  """The backup's row now, its archive by a task."""
  return {"task_id": Manager().request_backup_deletion(platform_backup(backup).id)}


@router.post("/{backup}/restore")
def restore_backup(backup: str):
  return {"task_id": Manager().request_platform_restore(backup)}
