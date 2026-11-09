from fastapi import APIRouter, Body, HTTPException

from services.db import Container, Domain
from services.domains.updates import domain_changed
from services.manager import Manager
from utils.api import generic_create, generic_delete, generic_get, generic_update, parse_api_data
from utils.common import get_env

router = APIRouter()


def domain_payload(domain: Domain) -> dict:
  """The domain with its full name, how it is reached, and the container it is linked to (none once that is
  deleted), with its worker and application."""
  container = domain.container
  application = container.application if container else None
  return {
      "id": domain.id,
      "name": domain.name,
      "type": domain.type,
      "port": domain.port,
      "service": domain.service,
      "pool": domain.pool,
      "cors_origins": domain.cors_origins,
      "status": Manager().domain_status(domain),
      "sync_error": domain.sync_error,
      "synced_hash": domain.synced_hash,
      "synced_at": domain.synced_at,
      "host": domain.host(get_env("DOMAIN")),
      "address": domain.address(get_env("DOMAIN")),
      "worker": container.worker_id if container else domain.worker_id,
      "container": container and {
          "id": container.id,
          "status": container.status,
          "worker": container.worker_id,
          "application": {
              "name": application.name,
              "label": application.label,
              "project": {"name": application.project.name, "label": application.project.label},
          },
      },
  }


def list_domains() -> dict:
  """Every domain, and every container one can be linked to, with its worker's subdomain and its application: the
  Domains page and its dialog. The `domains` topic."""
  return {
      "domains": [domain_payload(domain) for domain in Domain.select().order_by(Domain.type, Domain.name)],
      "containers": [
          {
              "id": container.id,
              "status": container.status,
              "worker": {"hostname": container.worker.hostname, "subdomain": container.worker.subdomain},
              "application": {
                  "name": container.application.name,
                  "label": container.application.label,
                  "project": {"name": container.application.project.name,
                              "label": container.application.project.label},
              },
          }
          for container in Container.select()
      ],
  }


def fields_of(domain_data: dict) -> dict:
  """The domain's own fields that were sent; an empty service or pool is none."""
  data = parse_api_data(domain_data, ["name", "type", "port", "service", "pool", "cors_origins"])
  for key in ("service", "pool"):
    if key in data:
      data[key] = data[key] or None
  return data


def container_of(domain_data: dict) -> Container:
  container = Container.get_or_none(Container.id == domain_data.get("container"))
  if container is None:
    raise HTTPException(status_code=404, detail=f"Container '{domain_data.get('container')}' not found")
  return container


@router.get("/")
def get():
  return list_domains()


@router.post("/")
def create_domain(domain_data: dict = Body(...)):
  if domain_data.get("container") is None:
    raise HTTPException(status_code=400, detail="A domain without a container is not allowed.")
  container = container_of(domain_data)
  result = generic_create(Domain, {**fields_of(domain_data), "container": container})
  domain = Domain.get_by_id(result["id"])
  Manager().request_discovery_sync()
  Manager().request_domain_sync(domain)
  domain_changed(domain)
  return domain_payload(Domain.get_by_id(domain.id))


@router.put("/{domain_id}")
def update_domain(domain_id: int, domain_data: dict = Body(...)):
  """Change a domain; what is not sent stays. A container on another worker moves an internal name there."""
  domain = generic_get(Domain, Domain.id == domain_id, return_model=True)
  before = domain.container
  data = fields_of(domain_data)
  if "container" in domain_data:
    data["container"] = container_of(domain_data)
  generic_update(Domain, domain, {**data, "status": "pending", "sync_error": None})
  Manager().request_discovery_sync()
  Manager().request_domain_sync(domain)
  domain_changed(domain, before)
  return domain_payload(Domain.get_by_id(domain_id))


@router.delete("/{domain_id}")
def delete_domain(domain_id: int):
  domain = generic_get(Domain, Domain.id == domain_id, return_model=True)
  result = generic_delete(Domain, domain)
  domain_changed(domain)
  Manager().request_discovery_sync()
  return result
