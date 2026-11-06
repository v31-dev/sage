from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from routes.domains import domain_payload
from services.db import APPLICATION_BUSY_STATUSES, Application, Container, Domain, Event, Project, Worker
from services.manager import Manager
from services.manager.application import container_changed
from utils.api import generic_create, generic_get, get_request_models
from utils.queue import OnConflict


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


def container_payload(container: Container) -> dict:
  data = model_to_dict(
      container, backrefs=True,
      only=(
          set(Container._meta.sorted_fields)
          | {Container.events}
          | set(Event._meta.sorted_fields)
          | set(Worker._meta.sorted_fields)
          | {Application.name, Application.label, Application.project, Project.name, Project.label}
      ),
  )
  data["domains"] = [domain_payload(domain) for domain in container.domains.order_by(Domain.type, Domain.name)]
  return data


@router.get("/")
def list_containers(request: Request):
  if "application" in request.state.models:
    return [container_payload(container) for container in request.state.models["application"].containers]
  elif "worker" in request.state.models:
    return [container_payload(container) for container in request.state.models["worker"].containers]


@router.post("/")
def create_container(request: Request, container_data: dict = Body(...)):
  if 'worker' in request.state.models:
    raise HTTPException(status_code=405, detail="Method not allowed.")

  application = request.state.models["application"]

  worker = Worker.get_or_none(Worker.hostname == container_data.get("worker"))
  if not worker:
    raise HTTPException(
        status_code=404,
        detail=f"Worker with hostname '{container_data.get('worker')}' not found",
    )

  if Container.select().where(
          (Container.application == application) & (Container.worker == worker)).exists():
    raise HTTPException(
        status_code=409, detail=f"Container already exists on worker '{worker.hostname}'.")

  result = generic_create(Container, {"application": application, "worker": worker})
  container = Container.get_by_id(result["id"])
  container_changed(container)
  return container_payload(container)


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

  # QUEUE admission would accept a duplicate, so reject a double-delete of this
  # specific container here; deletes of other containers still queue freely.
  if Manager().has_task(Manager().delete_container, {"container_id": container.id}):
    raise HTTPException(status_code=409, detail="This container is already being deleted.")

  Manager().add_task(
      task=Manager().delete_container,
      scopes={f"app:{application.qualified_name}"},
      params={"container_id": container.id, "force": force},
      executor="app",
      on_conflict=OnConflict.QUEUE,
  )

  return {"status": "OK"}
