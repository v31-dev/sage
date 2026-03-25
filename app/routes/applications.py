from fastapi import Request, APIRouter, Depends, Body, HTTPException
from playhouse.shortcuts import model_to_dict

from services.db import Application, Worker, Container
from utils.api import generic_get, generic_create, generic_update, generic_delete, generic_list
from routes.containers import router as container_router


def inject_application(request: Request):
  if not hasattr(request.state, 'models'):
    request.state.models = {}
  
  # Ensure project is already in models (from previous dependency)
  if 'project' not in request.state.models:
    raise HTTPException(status_code=400, detail="Project must be loaded before application.")
  
  # Fetch and store application
  application_name = request.path_params.get('application')
  request.state.models['application'] = generic_get(Application, 
                                                  (Application.name == application_name) & 
                                                  (Application.project == request.state.models['project']), 
                                                  return_model=True)
  
  return None

router = APIRouter()

@router.get("/")
def list_applications(request: Request):
  return generic_list(Application, Application.project == request.state.models['project'])

@router.post("/")
def create_application(request: Request, application_data: dict = Body(...), ):
  data = {
    'project': request.state.models['project'],
    'name': application_data.get('label'),
    'label': application_data.get('label'),
    'description': application_data.get('description'),
  }
  return generic_create(Application, data)

@router.get("/{application}", dependencies=[Depends(inject_application)])
def get_application(request: Request):
  return model_to_dict(request.state.models['application'], backrefs=True, max_depth=2)

@router.put("/{application}", dependencies=[Depends(inject_application)])
def update_application(request: Request, application_data: dict = Body(...)):
  data = {
    'label': application_data.get('label'),
    'description': application_data.get('description'),
    'image': application_data.get('image'),
    'env': application_data.get('env'),
    'args': application_data.get('args'),
    'type': application_data.get('type'),
    'repo': application_data.get('repo'),
    'path': application_data.get('path')

  }
  return generic_update(
    Application, 
    request.state.models['application'],
    data
  ) 

@router.delete("/{application}", dependencies=[Depends(inject_application)])
def delete_application(request: Request):
  return generic_delete(Application, request.state.models['application'])

@router.post("/{application}/get_available_workers", dependencies=[Depends(inject_application)])
def get_available_workers(request: Request):
  """Get list of all workers excluding those already in this application."""
  application = request.state.models['application']
  
  # Get all workers
  all_workers = list(Worker.select().dicts())
  
  # Get workers already used in this application's containers
  used_workers_query = (
    Container
      .select()
      .where(Container.application == application)
  )
  used_worker_hostnames = {w.worker.hostname for w in used_workers_query}
  
  # Filter out used workers
  available_workers = [w for w in all_workers if w['hostname'] not in used_worker_hostnames]
  
  return available_workers

@router.post("/{application}/deploy", dependencies=[Depends(inject_application)])
async def deploy_application(request: Request):
  """Trigger application deployment."""
  application = request.state.models['application']

  if application.container_count == 0:
    raise HTTPException(status_code=400, detail="Application has no containers to deploy.")
  
  request.app.state.rocketry["deploy_application"].run(application=application)

  return {"status": "OK"}

# Container routes
router.include_router(container_router, prefix="/{application}/containers", dependencies=[Depends(inject_application)])