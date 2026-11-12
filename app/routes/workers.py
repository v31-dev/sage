from typing import Literal

from fastapi import APIRouter, Body

from services.db.models import Worker
from services.manager import Manager
from services.metrics.series import PERIODS
from services.workers import commands
from utils.logging import NO_ACCESS_LOG

router = APIRouter()


@router.get("/")
def list_workers():
  return Manager().worker_list()


@router.post("/")
def add_worker(data: dict = Body(...)):
  """Adds the machine of the tailnet given by its hostname or Tailscale IP (`host`) as a worker with its `subdomain`."""
  hostname, task_id = Manager().request_add_worker(data.get("host"), data.get("subdomain"))
  return {"task_id": task_id, "hostname": hostname}


@router.get("/{hostname}")
def get_worker(hostname: str):
  return Manager().worker_page(hostname)


@router.put("/{hostname}")
def update_worker(hostname: str, data: dict = Body(...)):
  if commands.set_worker_subdomain(hostname, data.get("subdomain")):
    Manager().worker_subdomain_changed(hostname)
  return Manager().worker_page(hostname)


@router.delete("/{hostname}")
def remove_worker(hostname: str):
  return {"task_id": Manager().request_remove_worker(hostname)}


@router.post("/{hostname}/reboot")
def reboot_worker(hostname: str):
  return {"task_id": Manager().request_reboot(hostname)}


@router.post("/{hostname}/resync")
def resync_worker(hostname: str):
  """The worker's forced setup, which writes again everything Sage keeps on it."""
  if Worker.get_or_none(Worker.hostname == hostname) is None:
    raise LookupError("Worker not found.")
  return {"task_id": Manager().request_convergence(hostname, force=True)}


@router.get("/{hostname}/metrics", openapi_extra=NO_ACCESS_LOG)  # the graphs poll it every minute
def get_metrics(hostname: str, period: Literal[tuple(PERIODS)] = "1h"):
  """The graphs of a worker, or of the manager under its own name."""
  return Manager().worker_graphs(hostname, period)
