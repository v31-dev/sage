from fastapi import Request, APIRouter, Body, Depends, HTTPException
from services.db import Container, Worker
from utils.api import generic_get, generic_create, generic_delete, generic_list


def inject_container(request: Request):
  if not hasattr(request.state, 'models'):
    request.state.models = {}
  
  # Ensure project is already in models (from previous dependency)
  if 'project' not in request.state.models:
    raise HTTPException(status_code=400, detail="Project must be loaded before container.")
  
  if 'application' not in request.state.models:
    raise HTTPException(status_code=400, detail="Application must be loaded before container.")
  
  # Fetch and store container
  container_name = request.path_params.get('container')
  worker = Worker.get_or_none(Worker.hostname == container_name)
  if not worker:
    raise HTTPException(status_code=404, detail=f"Worker with hostname '{container_name}' not found")
  
  request.state.models['container'] = generic_get(Container, 
                                                  (Container.project == request.state.models['project']) & 
                                                  (Container.application == request.state.models['application']) & 
                                                  (Container.worker == worker), 
                                                  return_model=True)
  
  return None

router = APIRouter()

@router.get("/")
def list_containers(request: Request):
  return generic_list(Container, 
                      (Container.project == request.state.models['project']) & 
                      (Container.application == request.state.models['application']))

@router.post("/")
def create_container(request: Request, container_data: dict = Body(...)):
  data = {
    'project': request.state.models['project'],
    'application': request.state.models['application'],
  }

  worker = Worker.get_or_none(Worker.hostname == container_data.get('worker'))
  if not worker:
    raise HTTPException(status_code=404, detail=f"Worker with hostname '{container_data.get('worker')}' not found")
  else:
    data['worker'] = worker
  
  return generic_create(Container, data)

@router.delete("/{container}", dependencies=[Depends(inject_container)])
def delete_container(request: Request):
  return generic_delete(Container, request.state.models['container'])