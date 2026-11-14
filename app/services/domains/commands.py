from services.db.models import Container, Domain
from services.db.writes import get, merged, save_changes, trim, write
from services.domains.config import RESERVED_DOMAIN_NAMES, validate_port
from services.domains.updates import domain_changed
from utils.errors import Conflict
from values.names import normalize_origins

DOMAIN_FIELDS = ("name", "type", "port", "service", "pool", "cors_origins")


def _settle(config: dict, sent: set[str], domain_id: str, container_worker: str | None):
  """Derives and checks, in `config`, what depends on the `sent` fields: blank service and pool, the origins, the
  port, the namespace worker, and the name in that namespace."""
  for key in ("service", "pool"):
    if key in sent:
      config[key] = config[key] or None
  if sent & {"type", "cors_origins"}:
    config["cors_origins"] = None if config["type"] == "tcp" else normalize_origins(config["cors_origins"])
  if "port" in sent:
    validate_port(config["port"])
  if sent & {"type", "container"}:
    if config["type"] == "public":
      config["namespace_worker_id"] = None
    elif container_worker is not None:
      config["namespace_worker_id"] = container_worker
    elif config["namespace_worker_id"] is None:
      raise ValueError("An internal or TCP domain without a container is not allowed.")
  if sent & {"name", "type", "container"}:
    name, namespace = config["name"], config["namespace_worker_id"]
    if config["type"] == "public" and name in RESERVED_DOMAIN_NAMES:
      raise ValueError(f"'{name}' is reserved for Sage's own addresses.")
    in_namespace = Domain.namespace_worker == namespace if namespace else Domain.namespace_worker.is_null()
    if Domain.select().where((Domain.name == name) & in_namespace & (Domain.id != domain_id)).exists():
      raise Conflict(f"'{name}' is already a public domain." if namespace is None
                     else f"'{name}' is already an internal or TCP domain on worker {namespace}.")


def create_domain(container_id: str | None, fields: dict) -> Domain:
  if container_id is None:
    raise ValueError("A domain without a container is not allowed.")
  with write():
    container = get(Container, Container.id == container_id)
    draft = Domain()
    config = {**merged(draft, fields, DOMAIN_FIELDS), "container_id": container.id, "namespace_worker_id": None}
    if config["name"] is None:
      raise ValueError("name is required.")
    _settle(config, set(DOMAIN_FIELDS) | {"container"}, draft.id, container.worker_id)
    domain = Domain.create(id=draft.id, **config)
  domain_changed(domain)
  return domain


def patch_domain(domain_id: str, fields: dict) -> Domain:
  """Changes what is sent; `container` relinks it, which moves an internal or TCP name to that container's worker."""
  fields = dict(fields)
  relink = "container" in fields
  container_id = trim(fields.pop("container", None))
  with write():
    domain = get(Domain, Domain.id == domain_id)
    previous, previous_namespace = domain.container, domain.namespace_worker_id
    container = get(Container, Container.id == container_id) if relink else previous
    config = {**merged(domain, fields, DOMAIN_FIELDS), "container_id": container.id if container else None,
              "namespace_worker_id": domain.namespace_worker_id}
    _settle(config, set(fields) | ({"container"} if relink else set()), domain.id,
            container.worker_id if container else None)
    changed = save_changes(domain, config)
  if changed:
    domain_changed(domain, previous, previous_namespace)
  return domain


def delete_domain(domain_id: str):
  with write():
    domain = get(Domain, Domain.id == domain_id)
    domain.delete_instance()
  domain_changed(domain)
