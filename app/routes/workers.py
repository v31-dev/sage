from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from routes.containers import router as container_router
from services.db import Application, Container, Project, Worker
from services.logs import Logs
from services.manager import Manager
from services.metrics import Metrics
from services.reports import Reports
from services.tailscale import Tailscale
from utils.api import generic_get, get_request_models
from utils.common import get_env
from utils.queue import OnConflict

router = APIRouter()


def inject_worker(request: Request):
  models = get_request_models(request)

  hostname = request.path_params.get("hostname")

  if hostname == get_env("TS_NAME") or hostname == "*":
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
  return [{**worker, "status": Manager().worker_status(worker["hostname"], worker["online"])}
          for worker in Worker.select().dicts()]


@router.post("/")
def add_worker(request: Request, worker_data: dict = Body(...)):
  """Add a machine of the tailnet, given by hostname or Tailscale IP, as a worker."""
  host = str(worker_data.get("host") or "").strip()
  peer = Tailscale().find_peer(host) if host else None
  if peer is None:
    raise HTTPException(status_code=404, detail=f"No machine with hostname or IP '{host}' in the tailnet.")

  if Worker.get_or_none(Worker.hostname == peer.hostname):
    raise HTTPException(status_code=409, detail=f"Worker {peer.hostname} is already added.")
  if Manager().has_task(Manager().add_worker, {"worker_hostname": peer.hostname}):
    raise HTTPException(status_code=409, detail=f"Worker {peer.hostname} is already being added.")

  Manager().add_task(
      task=Manager().add_worker,
      scopes={"platform"},
      params={"worker_hostname": peer.hostname},
      executor="platform",
      task_id=request.state.task_id,
      on_conflict=OnConflict.QUEUE,
  )

  return {"status": "OK", "hostname": peer.hostname}


@router.get("/{hostname}", dependencies=[Depends(inject_worker)])
def get_worker(request: Request, hostname: str):
  worker = request.state.models["worker"]
  data = model_to_dict(
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
  data["infra"] = Reports().infra(hostname)
  data["status"] = Manager().worker_status(hostname, worker.online)
  return data


@router.get("/{hostname}/metrics", dependencies=[Depends(inject_worker)])
def get_metrics(request: Request, hostname: str, period: Literal["1h", "24h", "1w", "1m"] = "1h"):
  worker = get_request_models(request, ["worker"], none_allowed=True)["worker"]

  data = Metrics().query_period(hostname, period)

  if worker:
    data["meta"]["ip"] = worker.ip
  elif hostname == get_env("TS_NAME"):
    data["meta"]["ip"] = Tailscale().ip()

  return data


@router.get("/{hostname}/logs/{container}", dependencies=[Depends(inject_worker)])
def get_logs(hostname: str, container: str, search: str = "", from_ts: str = "", to_ts: str = ""):
  if hostname == "*":
    hostname = ""

  try:
    return Logs().query_logs(
        container, hostname=hostname, search=search, from_ts=from_ts, to_ts=to_ts
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"Invalid search query: {str(e)}")


@router.post("/{hostname}/reboot", dependencies=[Depends(inject_worker)])
def reboot_worker(request: Request, hostname: str):
  worker = request.state.models["worker"]

  if not worker:
    raise HTTPException(status_code=400, detail="Only a worker can be rebooted.")
  status = Manager().worker_status(hostname, worker.online)
  if status not in ("online", "not ready"):
    raise HTTPException(status_code=409, detail=f"Worker is {status}.")

  Manager().add_task(
      task=Manager().reboot_worker,
      scopes={"platform", "app"},
      params={"worker_hostname": worker.hostname},
      executor="platform",
      task_id=request.state.task_id,
      on_conflict=OnConflict.QUEUE,
  )

  return {"status": "OK"}


@router.delete("/{hostname}", dependencies=[Depends(inject_worker)])
def delete_worker(request: Request, hostname: str):
  worker = request.state.models["worker"]

  if not worker:
    raise HTTPException(status_code=404, detail="Worker not found")

  # An online worker is cleaned up when it goes, so its containers are deleted first; an offline one is
  # force-deleted with them.
  if worker.online and worker.containers.count() > 0:
    raise HTTPException(status_code=400, detail="Worker is online and has containers; delete them first.")
  if Manager().worker_status(hostname, worker.online) == "removing":
    raise HTTPException(status_code=409, detail="Worker is already being removed.")

  # platform+app scoped so it serializes with worker sync and deploys; QUEUE so a
  # deliberate removal waits behind an in-flight op instead of being dropped.
  Manager().add_task(
      task=Manager().remove_worker,
      scopes={"platform", "app"},
      params={"worker_hostname": worker.hostname},
      executor="platform",
      task_id=request.state.task_id,
      on_conflict=OnConflict.QUEUE,
  )

  return {"status": "OK"}


# Container routes
router.include_router(
    container_router,
    prefix="/{hostname}/containers",
    dependencies=[Depends(inject_worker)],
)
