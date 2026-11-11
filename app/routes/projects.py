from fastapi import APIRouter, Body

from routes.applications import router as applications_router
from services.applications import commands
from services.applications.payload import project_list, project_payload
from services.manager import Manager

router = APIRouter()


@router.get("/")
def list_projects():
  return project_list()


@router.post("/")
def create_project(data: dict = Body(...)):
  return project_payload(commands.create_project(data.get("label"), data.get("description")))


@router.get("/{project}")
def get_project(project: str):
  return Manager().project_page(project)


@router.put("/{project}")
def update_project(project: str, data: dict = Body(...)):
  return project_payload(commands.patch_project(project, data))


@router.delete("/{project}", status_code=204)
def delete_project(project: str):
  commands.delete_project(project)


router.include_router(applications_router, prefix="/{project}/applications")
