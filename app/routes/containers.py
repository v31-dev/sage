from fastapi import Request, APIRouter, Body
from services.db import Container
from utils.api import generic_get, generic_create, generic_update, generic_delete, generic_list


router = APIRouter()

@router.get("/")
def list_containers():
  return generic_list(Container)

@router.get("/{worker}")
def get_container(worker: str, request: Request):
  return generic_get(
    Container, 
    (Container.worker == worker) & (Container.application == request.state.parents.application)
  )

@router.post("/")
def create_container(request: Request, container_data: dict = Body(...)):
  container_data['application'] = request.state.parents.application
  container_data['project'] = request.state.parents.project
  return generic_create(Container, container_data)

@router.put("/{worker}")
def update_container(worker: str, request: Request, container_data: dict = Body(...)):
  return generic_update(
    Container, 
    (Container.worker == worker) & (Container.application == request.state.parents.application),
    container_data
  )

@router.delete("/{worker}")
def delete_container(worker: str, request: Request):
  return generic_delete(
    Container,
    (Container.worker == worker) & (Container.application == request.state.parents.application)
  )