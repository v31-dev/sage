from fastapi import APIRouter, Body

from services.applications import commands
from services.applications.lookup import find_application, find_container, find_volume
from services.applications.payload import volume_payload
from services.backups.download import download_link
from services.backups.payload import volume_backup, volume_backups
from services.manager import Manager

router = APIRouter()


@router.post("/")
def create_volume(project: str, application: str, data: dict = Body(...)):
  return volume_payload(commands.create_volume(find_application(project, application).id, data.get("name"),
                                               data.get("path"), data.get("backup_cron")))


@router.put("/{volume}")
def update_volume(project: str, application: str, volume: str, data: dict = Body(...)):
  return volume_payload(commands.patch_volume(find_volume(find_application(project, application).id, volume).id, data))


@router.delete("/{volume}", status_code=204)
def delete_volume(project: str, application: str, volume: str):
  commands.delete_volume(find_volume(find_application(project, application).id, volume).id)


@router.get("/{volume}/backups/")
def list_backups(project: str, application: str, volume: str):
  return volume_backups(find_volume(find_application(project, application).id, volume).id)


@router.post("/{volume}/backups/")
def back_up(project: str, application: str, volume: str):
  """A backup of the volume from each of the application's containers, all of them stopped."""
  owner = find_application(project, application)
  return {"task_ids": Manager().request_backup(owner.id, [find_volume(owner.id, volume).id])}


@router.get("/{volume}/backups/{backup}/download")
def download_backup(project: str, application: str, volume: str, backup: str):
  return download_link(volume_backup(find_volume(find_application(project, application).id, volume).id, backup))


@router.post("/{volume}/backups/{backup}/restore")
def restore_backup(project: str, application: str, volume: str, backup: str, data: dict = Body(...)):
  """The backup restored into a stopped container of the application (`container`), into the volume it was taken of
  or another of the application's (`volume`, by name)."""
  owner = find_application(project, application)
  archived = volume_backup(find_volume(owner.id, volume).id, backup)
  target = find_volume(owner.id, data.get("volume") or volume)
  return {"task_id": Manager().request_restore(find_container(owner.id, data.get("container")).id, archived.id, target.id)}
