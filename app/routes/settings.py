import re

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from services.certs import Certs
from services.cloudflare import Cloudflare
from services.db import Setting
from services.manager import Manager
from services.notification import Notifications
from services.s3 import S3
from services.settings import Settings
from utils.api import generic_get, generic_list, get_request_models, parse_api_data
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
  if setting_key == "tailscale":
    # Stored without the `tag:` prefix, which worker discovery adds back.
    worker_tag = (merged_setting_value.get("worker_tag") or "").strip().removeprefix("tag:") or None
    if worker_tag and not re.fullmatch(r"[A-Za-z0-9-]+", worker_tag):
      raise HTTPException(status_code=400, detail="Invalid worker tag. Use letters, numbers and dashes, e.g. worker.")

    Settings().set("tailscale", {"worker_tag": worker_tag})

  elif setting_key == "s3":
    if not S3().check_config(merged_setting_value):
      raise HTTPException(status_code=400, detail="Invalid S3 configuration. Please verify the provided settings.")

    Settings().set("s3", merged_setting_value)
    S3().load()

  elif setting_key == "notifications":
    if not Notifications().check_config(merged_setting_value):
      raise HTTPException(
          status_code=400,
          detail="Invalid notifications configuration. A configured channel did not accept a test message.")

    Settings().set("notifications", merged_setting_value)
    Notifications().load_notifications_config()

  elif setting_key == "cloudflare":
    domain_changed = "domain" in new_setting_value and new_setting_value["domain"] != old_setting_value.get("domain")
    admin_email_changed = "admin_email" in new_setting_value and new_setting_value["admin_email"] != old_setting_value.get("admin_email")
    api_token_changed = "api_token" in new_setting_value and new_setting_value["api_token"] != old_setting_value.get("api_token")
    account_id_changed = "account_id" in new_setting_value and new_setting_value["account_id"] != old_setting_value.get("account_id")
    cloudflare_config_changed = api_token_changed or account_id_changed or domain_changed
    cloudflare_setting_changed = cloudflare_config_changed or admin_email_changed
    certs_reload_needed = domain_changed or admin_email_changed
    traefik_refresh_needed = certs_reload_needed or api_token_changed

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

    if certs_reload_needed:
      Certs().load()

    if domain_changed:
      # New domain -> re-issue the wildcard cert and resync workers + routing.
      Manager().add_task(
          task=Manager().refresh_traefik,
          scopes={"platform", "app"},
          executor="platform",
          task_id=request.state.task_id,
          on_conflict=OnConflict.QUEUE,
      )

  return generic_get(Setting, (Setting.key == setting_key))


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
