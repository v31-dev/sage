from fastapi import Request, APIRouter, Depends, Body
from types import SimpleNamespace

from services.db import Application, Project, Worker, Container
from utils.api import generic_get, generic_create, generic_update, generic_delete, generic_list
from routes.containers import router as container_router


def inject_application(request: Request):
  if not hasattr(request.state, 'parents'):
    request.state.parents = SimpleNamespace()
  
  # Ensure project is already in parents (from previous dependency)
  if not hasattr(request.state.parents, 'project'):
    project_name = request.path_params.get('project')
    request.state.parents.project = generic_get(Project, Project.name == project_name, return_model=True)
  
  # Fetch and store application
  application_name = request.path_params.get('application')
  request.state.parents.application = generic_get(Application, 
                                                  (Application.name == application_name) & (Application.project == request.state.parents.project), 
                                                  return_model=True)
  
  return None

router = APIRouter()

@router.get("/")
def list_applications():
  return generic_list(Application)

@router.get("/{application}")
def get_application(application: str, request: Request):
  return generic_get(Application, (Application.name == application) & (Application.project == request.state.parents.project))

@router.post("/")
def create_application(request: Request, application_data: dict = Body(...), ):
  application_data['project'] = request.state.parents.project
  return generic_create(Application, application_data)

@router.put("/{application}")
def update_application(application: str, request: Request, application_data: dict = Body(...)):
  return generic_update(
    Application, 
    (Application.name == application) & (Application.project == request.state.parents.project),
    application_data
  ) 

@router.delete("/{application}")
def delete_application(application: str, request: Request):
  return generic_delete(
    Application,
    (Application.name == application) & (Application.project == request.state.parents.project)
  )

@router.post("/{application}/get_available_workers", dependencies=[Depends(inject_application)])
def get_available_workers(request: Request):
  """Get list of all workers excluding those already in this application."""
  project = request.state.parents.project
  application = request.state.parents.application
  
  # Get all workers
  all_workers = list(Worker.select().dicts())
  
  # Get workers already used in this project's application
  used_workers_query = (
    Container
      .select()
      .where(Container.project == project, 
             Container.application == application)
  )
  used_worker_hostnames = {w.worker.hostname for w in used_workers_query}
  
  # Filter out used workers
  available_workers = [w for w in all_workers if w['hostname'] not in used_worker_hostnames]
  
  return available_workers

# Container routes
router.include_router(container_router, prefix="/{application}/containers", dependencies=[Depends(inject_application)])