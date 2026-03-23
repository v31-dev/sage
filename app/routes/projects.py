from types import SimpleNamespace
from fastapi import APIRouter, Depends, Request, Body

from services.db import Project
from utils.api import generic_get, generic_create, generic_update, generic_delete, generic_list
from routes.applications import router as app_router


def inject_project(request: Request):
  if not hasattr(request.state, 'parents'):
    request.state.parents = SimpleNamespace()
  
  project_name = request.path_params.get('project')
  request.state.parents.project = generic_get(Project, Project.name == project_name, return_model=True)
  
  return None
  
router = APIRouter()

@router.get("/")
def list_projects():
  return generic_list(Project)

@router.get("/{project}")
def get_project(project: str):
  return generic_get(Project, Project.name == project)

@router.post("/")
def create_project(project_data: dict = Body(...)):
  return generic_create(Project, project_data)

@router.put("/{project}")
def update_project(project: str, project_data: dict = Body(...)):
  return generic_update(Project, Project.name == project, project_data) 

@router.delete("/{project}")
def delete_project(project: str):
  return generic_delete(Project, Project.name == project)

# Application routes
router.include_router(app_router, prefix="/{project}/applications", dependencies=[Depends(inject_project)])