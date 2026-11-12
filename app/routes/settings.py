from fastapi import APIRouter, Body, HTTPException

from services.db.models import Worker
from services.manager import Manager
from services.settings import Settings, setting_payload, validate_section

router = APIRouter()


@router.post("/pause")
def pause():
  """Pauses Sage before a stop or an upgrade: queued tasks wait, running ones finish, and changes are refused until
  resume. Not remembered across restarts."""
  Manager().pause_tasks()
  return {"state": Manager().task_state()}


@router.post("/resume")
def resume():
  Manager().resume_tasks()
  return {"state": Manager().task_state()}


@router.post("/resync_workers")
def resync_workers():
  """Each worker's resync, its forced setup."""
  return {"task_ids": [Manager().request_convergence(worker.hostname, force=True)
                       for worker in Worker.select(Worker.hostname).order_by(Worker.hostname)]}


def candidate(section: str, data: dict) -> dict:
  """The section's saved values with the fields sent (`value`) over them."""
  sent = data.get("value")
  if not isinstance(sent, dict):
    raise ValueError("value must hold the section's fields.")
  return {**setting_payload(section)["value"], **sent}


@router.get("/{section}")
def get_setting(section: str):
  return setting_payload(section)


@router.put("/{section}")
def save_setting(section: str, data: dict = Body(...)):
  """Saves the fields sent once the values pass the section's local rules; its provider is checked after, by a task."""
  values = candidate(section, data)
  validate_section(section, values)
  Settings().set(section, values)
  Manager().settings_saved(section)
  return setting_payload(section)


@router.post("/{section}/test")
def test_setting(section: str, data: dict = Body(...)):
  """Checks the values against the provider with the test's effects (S3 writes and deletes a marker object,
  notifications send a message), saving nothing."""
  values = candidate(section, data)
  try:
    return {"message": Manager().test_setting(section, values)}
  except Exception as error:
    raise HTTPException(status_code=400, detail=Manager().describe_check_error(error)[0])
