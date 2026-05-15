from typing import Literal
from fastapi import APIRouter, HTTPException, Depends, Request
from playhouse.shortcuts import model_to_dict

from services.db import Application, Container, Project, Worker
from services.metrics import Metrics
from services.tailscale import Tailscale
from services.manager import Manager
from routes.containers import router as container_router
from utils.common import get_env
from utils.api import get_request_models, generic_get


router = APIRouter()


def inject_worker(request: Request):
  models = get_request_models(request)

  hostname = request.path_params.get("hostname")

  if hostname == get_env("HOSTNAME") or hostname == "*":
    models["worker"] = None
  else:
    # Fetch and store worker
    models["worker"] = generic_get(
        Worker,
        Worker.hostname == hostname,
        return_model=True,
    )

  return None


@router.get("/")
def get():
  return list(Worker.select().dicts())


@router.get("/{hostname}", dependencies=[Depends(inject_worker)])
def get_worker(request: Request, hostname: str):
  worker = request.state.models["worker"]
  return model_to_dict(
      worker,
      backrefs=True,
      only=(
          set(Worker._meta.sorted_fields)
          | {Worker.containers}
          | set(Container._meta.sorted_fields)
          | {
              Application.name,
              Application.label,
              Application.project,
              Project.name,
              Project.label,
          }
      ),
  )


@router.get("/{hostname}/metrics", dependencies=[Depends(inject_worker)])
def get_metrics(request: Request, hostname: str, period: Literal["1h", "24h", "1w", "1m"] = "1h"):
  worker = get_request_models(request, ["worker"], none_allowed=True)["worker"]

  data = Metrics().query_period(hostname, period)

  if worker:
    data["meta"]["ip"] = worker.ip
  elif hostname == get_env("HOSTNAME"):
    data["meta"]["ip"] = Tailscale().ip()

  return data


@router.get("/{hostname}/logs/{container}", dependencies=[Depends(inject_worker)])
def get_logs(hostname: str, container: str, search: str = "", from_ts: str = "", to_ts: str = ""):
  if hostname == "*":
    hostname = ""

  try:
    return Metrics().query_logs(
        container, hostname=hostname, search=search, from_ts=from_ts, to_ts=to_ts
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"Invalid search query: {str(e)}")


@router.delete("/{hostname}", dependencies=[Depends(inject_worker)])
def delete_worker(request: Request, hostname: str):
  worker = request.state.models["worker"]

  if not worker:
    raise HTTPException(status_code=404, detail="Worker not found")

  # Worker should not have any containers before deletion
  if worker and worker.containers.count() > 0:
    raise HTTPException(status_code=400, detail="Worker has active containers and cannot be deleted.")

  # Delete worker and cleanup
  Manager().remove_worker(worker)

  return {"status": "deleted"}


# Container routes
router.include_router(
    container_router,
    prefix="/{hostname}/containers",
    dependencies=[Depends(inject_worker)],
)
