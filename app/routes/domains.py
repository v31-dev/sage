from fastapi import Request, APIRouter, Body, Depends, HTTPException

from services.db import Domain
from utils.api import generic_get, generic_create, generic_update, generic_delete, generic_list, parse_api_data


def inject_domain(request: Request):
  if not hasattr(request.state, 'models'):
    request.state.models = {}
  
  # Ensure project is already in models (from previous dependency)
  if 'project' not in request.state.models:
    raise HTTPException(status_code=400, detail="Project must be loaded before domain.")
  
  if 'application' not in request.state.models:
    raise HTTPException(status_code=400, detail="Application must be loaded before domain.")
  
  # Fetch and store domain (format type:name)
  try:
    domain_type, domain_name = request.path_params.get('domain').split(':')
  except ValueError:
    raise HTTPException(status_code=400, detail="Domain parameter must be in the format 'type:name'.")

  domain = generic_get(Domain, 
                       (Domain.application == request.state.models['application']) & 
                       (Domain.name == domain_name) &
                       (Domain.type == domain_type), 
                       return_model=True)
  if not domain:
    raise HTTPException(status_code=404, detail=f"Domain with name '{domain_name}' not found")
  
  request.state.models['domain'] = domain
  
  return None

router = APIRouter()

@router.get("/")
def list_domains(request: Request):
  return generic_list(Domain, 
                      (Domain.application == request.state.models['application']))

@router.post("/")
def create_domain(request: Request, domain_data: dict = Body(...)):
  data = {
    'application': request.state.models['application'],
    'name': domain_data.get('name'),
    'type': domain_data.get('type'),
    'port': domain_data.get('port')
  }
  return generic_create(Domain, data)

@router.put("/{domain}", dependencies=[Depends(inject_domain)])
def update_domain(request: Request, domain_data: dict = Body(...)):
  data = parse_api_data(domain_data, ['name', 'type', 'port'])
  return generic_update(Domain, request.state.models['domain'], data) 

@router.delete("/{domain}", dependencies=[Depends(inject_domain)])
def delete_domain(request: Request):
  """Trigger domain deletion."""
  domain = request.state.models['domain']
  return generic_delete(Domain, domain)