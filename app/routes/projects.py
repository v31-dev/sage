from fastapi import APIRouter, Depends, Request, Body
from playhouse.shortcuts import model_to_dict

from services.db import Project
from utils.api import generic_get, generic_create, generic_update, generic_delete, generic_list
from routes.applications import router as app_router


def inject_project(request: Request):
  if not hasattr(request.state, 'models'):
    request.state.models = {}
  
  project_name = request.path_params.get('project')
  request.state.models['project'] = generic_get(Project, Project.name == project_name, return_model=True)
  
  return None
  
router = APIRouter()

@router.get("/")
def list_projects():
  return generic_list(Project)

@router.post("/")
def create_project(project_data: dict = Body(...)):
  data = {
    'name': project_data.get('label'),
    'label': project_data.get('label'),
    'description': project_data.get('description'),
  }

  return generic_create(Project, data)

@router.get("/{project}", dependencies=[Depends(inject_project)])
def get_project(request: Request):
  return model_to_dict(request.state.models['project'], backrefs=True)

@router.put("/{project}", dependencies=[Depends(inject_project)])
def update_project(request: Request, project_data: dict = Body(...)):
  return generic_update(Project, request.state.models['project'], project_data) 

@router.delete("/{project}", dependencies=[Depends(inject_project)])
def delete_project(request: Request):
  return generic_delete(Project, request.state.models['project'])

# Application routes
router.include_router(app_router, prefix="/{project}/applications", dependencies=[Depends(inject_project)])