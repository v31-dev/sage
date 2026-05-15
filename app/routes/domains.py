from fastapi import APIRouter, Body, Depends, HTTPException, Request

from services.db import Domain
from utils.api import (
    get_request_models,
    generic_create,
    generic_delete,
    generic_get,
    generic_list,
    generic_update,
    parse_api_data,
)


def inject_domain(request: Request):
  models = get_request_models(request, ["project", "application"])
  application = models["application"]

  # Fetch and store domain (format type:name)
  try:
    domain_type, domain_name = request.path_params.get("domain").split(":")
  except ValueError:
    raise HTTPException(
        status_code=400,
        detail="Domain parameter must be in the format 'type:name'.",
    )

  domain = generic_get(
      Domain,
      (Domain.application == application)
      & (Domain.name == domain_name)
      & (Domain.type == domain_type),
      return_model=True,
  )
  if not domain:
    raise HTTPException(status_code=404, detail=f"Domain with name '{domain_name}' not found")

  models["domain"] = domain

  return None


router = APIRouter()


@router.get("/")
def list_domains(request: Request):
  return generic_list(Domain, (Domain.application == request.state.models["application"]))


@router.post("/")
def create_domain(request: Request, domain_data: dict = Body(...)):
  data = {
      "application": request.state.models["application"],
      "name": domain_data.get("name"),
      "type": domain_data.get("type"),
      "port": domain_data.get("port"),
  }
  return generic_create(Domain, data)


@router.put("/{domain}", dependencies=[Depends(inject_domain)])
def update_domain(request: Request, domain_data: dict = Body(...)):
  data = parse_api_data(domain_data, ["name", "type", "port"])
  return generic_update(Domain, request.state.models["domain"], data)


@router.delete("/{domain}", dependencies=[Depends(inject_domain)])
def delete_domain(request: Request):
  """Trigger domain deletion."""
  domain = request.state.models["domain"]
  return generic_delete(Domain, domain)
