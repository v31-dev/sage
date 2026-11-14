from fastapi import APIRouter, Body

from services.domains import commands
from services.manager import Manager

router = APIRouter()


@router.get("/")
def list_domains():
  return Manager().domains_page()


@router.post("/")
def create_domain(data: dict = Body(...)):
  """A name for a container (`container`, by id)."""
  domain = commands.create_domain(data.pop("container", None), data)
  Manager().request_domain_applies([domain.id])
  return Manager().domain_payloads([domain])[0]


@router.put("/{domain}")
def update_domain(domain: str, data: dict = Body(...)):
  """Changes what is sent; `container` relinks it."""
  edited = commands.patch_domain(domain, data)
  Manager().domain_edited(edited.id)
  return Manager().domain_payloads([edited])[0]


@router.delete("/{domain}", status_code=204)
def delete_domain(domain: str):
  commands.delete_domain(domain)
  Manager().request_domain_reconciliation()
