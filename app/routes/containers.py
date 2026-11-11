from fastapi import APIRouter, Body

from services.applications import commands
from services.applications.lookup import find_application, find_container
from services.manager import Manager

router = APIRouter()


@router.post("/")
def create_container(project: str, application: str, data: dict = Body(...)):
  container = commands.attach_container(find_application(project, application).id, data.get("worker"))
  return Manager().container_payloads([container])[0]


@router.delete("/{container}")
def delete_container(project: str, application: str, container: str, force: bool = False):
  find_container(find_application(project, application).id, container)
  return {"task_id": Manager().request_delete(container, force)}


@router.post("/{container}/deploy")
def deploy_container(project: str, application: str, container: str):
  find_container(find_application(project, application).id, container)
  return {"task_id": Manager().request_deploy(container)}


@router.post("/{container}/stop")
def stop_container(project: str, application: str, container: str):
  find_container(find_application(project, application).id, container)
  return {"task_id": Manager().request_stop(container)}


@router.post("/{container}/cancel/{operation}")
def cancel_request(project: str, application: str, container: str, operation: str):
  find_container(find_application(project, application).id, container)
  return {"task_id": Manager().request_cancel(container, operation)}
