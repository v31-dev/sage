from fastapi import APIRouter, HTTPException, Depends, Request, Body
from playhouse.shortcuts import model_to_dict

from services.cloudflare import Cloudflare
from services.db import Setting
from services.manager import Manager
from services.notification import Notifications
from services.settings import Settings
from services.s3 import S3
from utils.api import (
    get_request_models,
    generic_list,
    generic_get,
    parse_api_data,
)
from utils.common import DOMAIN_RE, EMAIL_RE
from utils.queue import OnConflict

router = APIRouter()


def inject_setting(request: Request):
  models = get_request_models(request)

  # Fetch and store setting
  setting_name = request.path_params.get("setting")
  models["setting"] = generic_get(
      Setting,
      (Setting.key == setting_name),
      return_model=True,
  )

  return None


@router.get("/")
def get():
  return generic_list(Setting)


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
    if not S3().check_config(merged_setting_value):
      raise HTTPException(status_code=400, detail="Invalid S3 configuration. Please verify the provided settings.")

    Settings().set("s3", merged_setting_value)
    S3().load()

  elif setting_key == "notifications":
    Settings().set("notifications", merged_setting_value)
    Notifications().load_notifications_config()

  elif setting_key == "cloudflare":
    domain_changed = "domain" in new_setting_value and new_setting_value["domain"] != old_setting_value.get("domain")
    admin_email_changed = "admin_email" in new_setting_value and new_setting_value["admin_email"] != old_setting_value.get("admin_email")
    api_token_changed = "api_token" in new_setting_value and new_setting_value["api_token"] != old_setting_value.get("api_token")
    account_id_changed = "account_id" in new_setting_value and new_setting_value["account_id"] != old_setting_value.get("account_id")
    cloudflare_config_changed = api_token_changed or account_id_changed or domain_changed
    cloudflare_setting_changed = cloudflare_config_changed or admin_email_changed
    traefik_refresh_needed = admin_email_changed or domain_changed or api_token_changed

    if domain_changed and not DOMAIN_RE.fullmatch(merged_setting_value["domain"]):
      raise HTTPException(status_code=400, detail="Invalid domain format in Cloudflare settings.")

    if admin_email_changed and not EMAIL_RE.fullmatch(merged_setting_value["admin_email"]):
      raise HTTPException(status_code=400, detail="Invalid email format in Cloudflare settings.")

    # Validate Cloudflare and trigger changes if needed
    if cloudflare_config_changed and not Cloudflare().check_config(merged_setting_value):
      raise HTTPException(status_code=400, detail="Invalid Cloudflare configuration. Please verify the API token, account ID, and domain.")

    if traefik_refresh_needed and Manager().is_busy({"platform", "app"}):
      raise HTTPException(
          status_code=409,
          detail="A platform operation is already in progress. Wait for it to finish before changing the Cloudflare domain, admin email, or API token again.",
      )

    if cloudflare_setting_changed:
      Settings().set("cloudflare", merged_setting_value)

    if cloudflare_config_changed:
      Cloudflare().load()

    if traefik_refresh_needed:
      # Manager Traefik static config / token file rewritten and container restarted;
      # worker Traefik config resynced when email or domain change.
      Manager().add_task(
          task=Manager().refresh_traefik,
          scopes={"platform", "app"},
          params={
              "admin_email_changed": admin_email_changed,
              "domain_changed": domain_changed,
              "api_token_changed": api_token_changed,
          },
          executor="platform",
          task_id=request.state.task_id,
          on_conflict=OnConflict.QUEUE,
      )

  return generic_get(Setting, (Setting.key == setting_key))


@router.post("/restart")
async def restart():
  """
  Trigger a restart of the full compose stack via the Docker API socket. Pending
  queued work is cancelled and the restart waits (with priority) for in-flight
  platform/app operations to finish before the process is replaced. Progress is
  not tracked once the restart fires.
  """
  Manager().cancel_all_tasks()
  Manager().add_task(
      task=Manager().restart,
      params={"all": True},
      scopes={"platform", "app", "common", "metrics"},
      executor="platform",
      on_conflict=OnConflict.QUEUE,
      priority=True,
  )
  return {"message": "Restart initiated."}


@router.post("/resync_traefik")
async def resync_traefik(request: Request):
  """
  Force a full Traefik state resync to manager and workers. Reuses the
  refresh_traefik task with all change flags set; cert re-issuance is expected.
  """
  if not Manager().add_task(
      task=Manager().refresh_traefik,
      scopes={"platform", "app"},
      params={
          "admin_email_changed": True,
          "domain_changed": True,
          "api_token_changed": True,
      },
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
