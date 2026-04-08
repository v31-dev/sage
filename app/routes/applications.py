from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from routes.containers import router as container_router
from routes.domains import router as domain_router
from services.db import Application, Container, Worker
from services.metrics import Metrics
from utils.api import (
    generic_create,
    generic_delete,
    generic_get,
    generic_list,
    generic_update,
    parse_api_data,
)


def inject_application(request: Request):
    if not hasattr(request.state, "models"):
        request.state.models = {}

    # Ensure project is already in models (from previous dependency)
    if "project" not in request.state.models:
        raise HTTPException(status_code=400, detail="Project must be loaded before application.")

    # Fetch and store application
    application_name = request.path_params.get("application")
    request.state.models["application"] = generic_get(
        Application,
        (Application.name == application_name)
        & (Application.project == request.state.models["project"]),
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
    data["name"] = data["label"]
    return generic_create(Application, data)


@router.get("/{application}", dependencies=[Depends(inject_application)])
def get_application(request: Request):
    return model_to_dict(request.state.models["application"], backrefs=True, max_depth=2)


@router.put("/{application}", dependencies=[Depends(inject_application)])
def update_application(request: Request, application_data: dict = Body(...)):
    data = parse_api_data(
        application_data,
        ["label", "description", "image", "env", "args", "type", "repo", "path"],
    )
    return generic_update(Application, request.state.models["application"], data)


@router.delete("/{application}", dependencies=[Depends(inject_application)])
def delete_application(request: Request):
    return generic_delete(Application, request.state.models["application"])


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

    if application.status in ["deploying", "stopping"]:
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

    request.app.state.rocketry["deploy_application"].run(application=application)

    return {"status": "OK"}


@router.post("/{application}/stop", dependencies=[Depends(inject_application)])
def stop_application(request: Request):
    """Trigger application stop."""
    application = request.state.models["application"]

    if application.status in ["deploying", "stopping"]:
        raise HTTPException(status_code=409, detail=f"Application is already {application.status}.")

    if application.container_count == 0:
        raise HTTPException(status_code=400, detail="Application has no containers to stop.")

    request.app.state.rocketry["stop_application"].run(application=application)

    return {"status": "OK"}


@router.post("/{application}/metrics", dependencies=[Depends(inject_application)])
def get_metrics(request: Request, period: Literal["1h", "24h", "1w", "1m"] = "1h"):
    application = request.state.models["application"]
    container_name = f"{application.project.name}-{application.name}"

    data = []
    for container in application.containers:
        container_metrics = Metrics().query_container_period(
            container.worker.hostname, container_name, period
        )
        data.append({"hostname": container.worker.hostname, "metrics": container_metrics})

    return data


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
