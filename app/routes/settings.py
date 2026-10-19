from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from services.db import Setting
from services.manager import Manager
from services.notification import Notifications
from services.s3 import S3
from services.settings import SETTING_DEFINITIONS, Settings
from utils.api import generic_get, generic_list, get_request_models, parse_api_data
from utils.queue import OnConflict

router = APIRouter()


def inject_setting(request: Request):
  models = get_request_models(request)

  # Fetch and store the setting; only the defined keys exist.
  setting_name = request.path_params.get("setting")
  if setting_name not in SETTING_DEFINITIONS:
    raise HTTPException(status_code=404, detail=f"Unknown setting: {setting_name}")
  models["setting"] = generic_get(
      Setting,
      (Setting.key == setting_name),
      return_model=True,
  )

  return None


@router.get("/")
def get():
  return generic_list(Setting, Setting.key.in_(list(SETTING_DEFINITIONS)))


@router.get("/status")
def get_status():
  return Manager().settings_status()


@router.get("/{setting}", dependencies=[Depends(inject_setting)])
def get_setting(request: Request):
  return model_to_dict(request.state.models["setting"])


@router.put("/{setting}", dependencies=[Depends(inject_setting)])
def update_setting(request: Request, setting_data: dict = Body(...)):
  data = parse_api_data(setting_data, ["value"])

  if "value" not in data:
    raise HTTPException(status_code=400, detail="Missing 'value' field in request body.")

  setting_model = request.state.models["setting"]
  setting_key = setting_model.key
  old_setting_value = setting_model.value
  new_setting_value = data["value"]
  merged_setting_value = dict(old_setting_value)
  merged_setting_value.update(new_setting_value)

  # Validate the settings before update
  if setting_key == "s3":
    try:
      S3().check_config(merged_setting_value)
    except Exception as e:
      raise HTTPException(status_code=400, detail=f"S3 check failed: {Manager().describe_check_error(e)[0]}")

    Settings().set("s3", merged_setting_value)
    S3().load()
    # The bucket or path may hold backups this manager hasn't listed yet.
    Manager().add_task(
        task=Manager().discover_s3_backups,
        scopes={"platform", "app"},
        executor="platform",
    )

  elif setting_key == "notifications":
    try:
      Notifications().check_config(merged_setting_value)
    except Exception as e:
      raise HTTPException(status_code=400, detail=Manager().describe_check_error(e)[0])

    Settings().set("notifications", merged_setting_value)
    Notifications().load_notifications_config()

  if Settings().is_valid(setting_key):
    Manager().add_task(
        task=Manager().check_setting,
        name=f"check_{setting_key}",
        params={"key": setting_key},
        scopes={f"common:{setting_key}"},
        executor="common",
        on_conflict=OnConflict.REPLACE,
    )

  return generic_get(Setting, (Setting.key == setting_key))


@router.post("/{setting}/test", dependencies=[Depends(inject_setting)])
def test_setting(request: Request, setting_data: dict = Body(...)):
  """Check the given values (saved or not) against the provider, without saving them."""
  setting_model = request.state.models["setting"]
  value = dict(setting_model.value)
  value.update(parse_api_data(setting_data, ["value"]).get("value") or {})

  try:
    return {"message": Manager().test_setting(setting_model.key, value)}
  except Exception as e:
    raise HTTPException(status_code=400, detail=Manager().describe_check_error(e)[0])


@router.post("/pause")
def pause():
  """Pause Sage before stopping it: queued tasks wait, running ones finish, and user
  changes are refused until resume. Not remembered across restarts."""
  Manager().pause_tasks()
  return {"state": Manager().task_state()}


@router.post("/resume")
def resume():
  Manager().resume_tasks()
  return {"state": Manager().task_state()}


@router.post("/resync_traefik")
async def resync_traefik(request: Request):
  """
  Force a full Traefik state resync to manager and workers via the
  refresh_traefik task (re-pushes the current cert + configs; re-issues only if
  the cert is missing or expiring).
  """
  if not Manager().add_task(
      task=Manager().refresh_traefik,
      scopes={"platform", "app"},
      executor="platform",
      task_id=request.state.task_id,
  ):
    raise HTTPException(
        status_code=409,
        detail="A platform operation is already in progress. Wait for it to finish before resyncing.",
    )

  return {"message": "Traefik resync initiated."}


@router.post("/resync_workers")
async def resync_workers(request: Request):
  """
  Queue a force re-sync of all online workers. REPLACE supersedes any pending
  sync so the latest request wins, and waits behind a running sync rather than
  being dropped.
  """
  Manager().add_task(
      task=Manager().sync_workers,
      scopes={"platform"},
      params={"force": True},
      executor="platform",
      task_id=request.state.task_id,
      on_conflict=OnConflict.REPLACE,
  )
  return {"message": "Worker resync queued."}
