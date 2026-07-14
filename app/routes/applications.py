from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from routes.volumes import router as volume_router
from routes.containers import router as container_router
from routes.domains import router as domain_router
from services.db import (
    APPLICATION_BUSY_STATUSES,
    APPLICATION_STOP_ELIGIBLE_STATUSES,
    Application,
    Container,
    Worker,
)
from services.manager import Manager
from services.metrics import Metrics
from utils.api import (
    get_request_models,
    generic_create,
    generic_delete,
    generic_get,
    generic_list,
    generic_update,
    parse_api_data,
)
from utils.db import AlphaNumericField


def inject_application(request: Request):
  models = get_request_models(request, ["project"])
  project = models["project"]

  # Fetch and store application
  application_name = request.path_params.get("application")
  models["application"] = generic_get(
      Application,
      (Application.name == application_name) & (Application.project == project),
      return_model=True,
  )

  return None


router = APIRouter()


@router.get("/")
def list_applications(request: Request):
  return generic_list(Application, Application.project == request.state.models["project"])


@router.post("/")
def create_application(
    request: Request,
    application_data: dict = Body(...),
):
  data = parse_api_data(application_data, ["label", "description"])
  data["project"] = request.state.models["project"]
  data["name"] = AlphaNumericField.clean(data["label"])
  return generic_create(Application, data)


@router.get("/{application}", dependencies=[Depends(inject_application)])
def get_application(request: Request):
  return model_to_dict(request.state.models["application"], backrefs=True, max_depth=2)


@router.put("/{application}", dependencies=[Depends(inject_application)])
def update_application(request: Request, application_data: dict = Body(...)):
  data = parse_api_data(
      application_data,
      ["label", "description", "image", "env", "args", "build_secrets", "command", "type", "repo", "path"],
  )

  return generic_update(Application, request.state.models["application"], data)


@router.delete("/{application}", dependencies=[Depends(inject_application)])
def delete_application(request: Request):
  application = request.state.models["application"]

  if application.status in APPLICATION_BUSY_STATUSES:
    raise HTTPException(status_code=409, detail=f"Application is already {application.status}.")
  return generic_delete(Application, application)


@router.post("/{application}/get_available_workers", dependencies=[Depends(inject_application)])
def get_available_workers(request: Request):
  """Get list of all workers excluding those already in this application."""
  application = request.state.models["application"]

  # Get all workers
  all_workers = list(Worker.select().dicts())

  # Get workers already used in this application's containers
  used_workers_query = Container.select().where(Container.application == application)
  used_worker_hostnames = {w.worker.hostname for w in used_workers_query}

  # Filter out used workers
  available_workers = [w for w in all_workers if w["hostname"] not in used_worker_hostnames]

  return available_workers


@router.post("/{application}/deploy", dependencies=[Depends(inject_application)])
def deploy_application(request: Request):
  """Trigger application deployment."""
  application = request.state.models["application"]

  if application.status in APPLICATION_BUSY_STATUSES:
    raise HTTPException(status_code=409, detail=f"Application is already {application.status}.")

  if application.container_count == 0:
    raise HTTPException(status_code=400, detail="Application has no containers to deploy.")

  if application.type == "docker" and (
      application.image is None or application.image.strip() == ""
  ):
    raise HTTPException(
        status_code=400, detail="Docker applications must have an image specified."
    )

  if application.type == "git" and (
      application.repo is None
      or application.repo.strip() == ""
      or application.path is None
      or application.path.strip() == ""
  ):
    raise HTTPException(
        status_code=400,
        detail="Git applications must have a repo & path specified.",
    )

  if not Manager().add_task(
      task=Manager().deploy_application,
      scopes={f"app:{application.qualified_name}"},
      params={"application_id": application.id},
      executor="app",
      task_id=request.state.task_id,
  ):
    raise HTTPException(status_code=409, detail="Application already has an operation in progress.")

  return {"status": "OK"}


@router.post("/{application}/stop", dependencies=[Depends(inject_application)])
def stop_application(request: Request):
  """Trigger application stop."""
  application = request.state.models["application"]

  if application.status in APPLICATION_BUSY_STATUSES:
    raise HTTPException(status_code=409, detail=f"Application is already {application.status}.")

  if application.status not in APPLICATION_STOP_ELIGIBLE_STATUSES:
    raise HTTPException(
        status_code=409,
        detail="Application must be active or error before stop.",
    )

  if application.container_count == 0:
    raise HTTPException(status_code=400, detail="Application has no containers to stop.")

  if not Manager().add_task(
      task=Manager().stop_application,
      scopes={f"app:{application.qualified_name}"},
      params={"application_id": application.id},
      executor="app",
      task_id=request.state.task_id,
  ):
    raise HTTPException(status_code=409, detail="Application already has an operation in progress.")

  return {"status": "OK"}


@router.post("/{application}/metrics", dependencies=[Depends(inject_application)])
def get_metrics(request: Request, period: Literal["1h", "24h", "1w", "1m"] = "1h"):
  application = request.state.models["application"]

  data = []
  for container in application.containers:
    container_metrics = Metrics().query_container_period(
        container.worker.hostname, application.qualified_name, period
    )
    data.append({"hostname": container.worker.hostname, "metrics": container_metrics})

  return data


# Volume routes
router.include_router(
    volume_router,
    prefix="/{application}/volumes",
    dependencies=[Depends(inject_application)],
)

# Container routes
router.include_router(
    container_router,
    prefix="/{application}/containers",
    dependencies=[Depends(inject_application)],
)

# Domain routes
router.include_router(
    domain_router,
    prefix="/{application}/domains",
    dependencies=[Depends(inject_application)],
)
