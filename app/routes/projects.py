from fastapi import APIRouter, Body, Depends, HTTPException, Request
from playhouse.shortcuts import model_to_dict

from routes.applications import router as app_router
from services.db import Project
from utils.api import generic_create, generic_delete, generic_get, generic_list, generic_update, get_request_models, parse_api_data
from utils.db import AlphaNumericField


def inject_project(request: Request):
  models = get_request_models(request)

  project_name = request.path_params.get("project")
  models["project"] = generic_get(
      Project, Project.name == project_name, return_model=True
  )

  return None


router = APIRouter()


@router.get("/")
def list_projects():
  return generic_list(Project)


@router.post("/")
def create_project(project_data: dict = Body(...)):
  data = parse_api_data(project_data, ["label", "description"])
  data["name"] = AlphaNumericField.clean(data["label"])
  return generic_create(Project, data)


@router.get("/{project}", dependencies=[Depends(inject_project)])
def get_project(request: Request):
  return model_to_dict(request.state.models["project"], backrefs=True, max_depth=1)


@router.put("/{project}", dependencies=[Depends(inject_project)])
def update_project(request: Request, project_data: dict = Body(...)):
  data = parse_api_data(project_data, ["label", "description", "env"])
  return generic_update(Project, request.state.models["project"], data)


@router.delete("/{project}", dependencies=[Depends(inject_project)])
def delete_project(request: Request):
  project = request.state.models["project"]

  if project.application_count > 0:
    raise HTTPException(
        status_code=409,
        detail="Project has applications and cannot be deleted. Delete its applications first.",
    )
  return generic_delete(Project, project)


# Application routes
router.include_router(
    app_router, prefix="/{project}/applications", dependencies=[Depends(inject_project)]
)
