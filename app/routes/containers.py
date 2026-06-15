from fastapi import APIRouter, Body, Depends, HTTPException, Request

from services.db import APPLICATION_BUSY_STATUSES, Container, Worker
from services.manager import Manager
from utils.api import (
    get_request_models,
    generic_create,
    generic_get,
    generic_list,
    generic_update,
    parse_api_data,
)



# Container is a sub-route of Application & Worker
def inject_container(request: Request):
  models = get_request_models(request)

  # The nested `{container}` segment means different things depending on the
  # parent route: under applications it refers to the worker hostname, while
  # under workers it refers to the container id.
  if "application" in models:
    worker_hostname = request.path_params.get("container")
    worker = Worker.get_or_none(Worker.hostname == worker_hostname)
    if not worker:
      raise HTTPException(
          status_code=404,
          detail=f"Worker with hostname '{worker_hostname}' not found",
      )

    models["container"] = generic_get(
        Container,
        (Container.application == models["application"]) & (Container.worker == worker),
        return_model=True,
    )
  elif "worker" in models:
    container_id = request.path_params.get("container")
    models["container"] = generic_get(
        Container,
        (Container.id == container_id) & (Container.worker == models["worker"]),
        return_model=True,
    )
  else:
    raise HTTPException(status_code=400, detail="Application must be loaded before container.")

  return None


router = APIRouter()

CONTAINER_BUSY_STATUSES = APPLICATION_BUSY_STATUSES


@router.get("/")
def list_containers(request: Request):
  if "application" in request.state.models:
    return generic_list(Container, Container.application == request.state.models["application"])
  elif "worker" in request.state.models:
    return generic_list(Container, Container.worker == request.state.models["worker"])


@router.post("/")
def create_container(request: Request, container_data: dict = Body(...)):
  if 'worker' in request.state.models:
    raise HTTPException(status_code=405, detail="Method not allowed.")

  data = {
      "application": request.state.models["application"],
  }

  worker = Worker.get_or_none(Worker.hostname == container_data.get("worker"))
  if not worker:
    raise HTTPException(
        status_code=404,
        detail=f"Worker with hostname '{container_data.get('worker')}' not found",
    )
  else:
    data["worker"] = worker

  if "domain_tag" in container_data:
    data["domain_tag"] = container_data.get("domain_tag")

  return generic_create(Container, data)


@router.put("/{container}", dependencies=[Depends(inject_container)])
def update_container(request: Request, container_data: dict = Body(...)):
  if 'worker' in request.state.models:
    raise HTTPException(status_code=405, detail="Method not allowed.")

  data = parse_api_data(
      container_data,
      ["domain_tag"],
  )
  return generic_update(Container, request.state.models["container"], data)


@router.delete("/{container}", dependencies=[Depends(inject_container)])
def delete_container(request: Request, force: bool = False):
  """Trigger container deletion."""
  if 'worker' in request.state.models:
    raise HTTPException(status_code=405, detail="Method not allowed.")

  application = request.state.models["application"]
  container = request.state.models["container"]

  if application.status in CONTAINER_BUSY_STATUSES:
    raise HTTPException(status_code=409, detail=f"Application is currently {application.status}.")

  if container.status in CONTAINER_BUSY_STATUSES:
    raise HTTPException(status_code=409, detail=f"Container is currently {container.status}.")

  if not Manager().add_task(
      task=Manager().delete_container,
      scopes={f"app:{application.qualified_name}"},
      params={"container_id": container.id, "force": force},
      executor="app",
      task_id=request.state.task_id,
  ):
    raise HTTPException(status_code=409, detail="Application already has an operation in progress.")

  return {"status": "OK"}
