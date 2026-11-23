from services.db.models import Application, Container, Domain, DomainCaller
from services.db.writes import get, merged, save_changes, write
from services.domains.config import RESERVED_DOMAIN_NAMES, validate_port
from services.domains.updates import domain_changed
from utils.errors import Conflict
from values.names import normalize_origins

DOMAIN_FIELDS = ("name", "type", "port", "service", "pool", "cors_origins")


def _settle(config: dict, sent: set[str], domain_id: str, container_worker: str):
  """Derives and checks, in `config`, what depends on the `sent` fields: blank service and pool, the origins, the
  port, the namespace worker (its container's, none for a public one), and the name in that namespace."""
  for key in ("service", "pool"):
    if key in sent:
      config[key] = config[key] or None
  if sent & {"type", "cors_origins"}:
    config["cors_origins"] = None if config["type"] == "tcp" else normalize_origins(config["cors_origins"])
  if "port" in sent:
    validate_port(config["port"])
  if "type" in sent:
    config["namespace_worker_id"] = None if config["type"] == "public" else container_worker
  if sent & {"name", "type"}:
    name, namespace = config["name"], config["namespace_worker_id"]
    if config["type"] == "public" and name in RESERVED_DOMAIN_NAMES:
      raise ValueError(f"'{name}' is reserved for Sage's own addresses.")
    in_namespace = Domain.namespace_worker == namespace if namespace else Domain.namespace_worker.is_null()
    if Domain.select().where((Domain.name == name) & in_namespace & (Domain.id != domain_id)).exists():
      raise Conflict(f"'{name}' is already a public domain." if namespace is None
                     else f"'{name}' is already an internal or TCP domain on worker {namespace}.")


def _write_callers(domain: Domain, callers) -> bool:
  """Writes `callers`, the ids of the applications allowed to call `domain`, when sent (None when not), and drops
  them when the domain is public; whether they changed."""
  if callers is None:
    callers = [] if domain.type == "public" else None
  elif not isinstance(callers, list) or not all(isinstance(application_id, int) and not isinstance(application_id, bool) for application_id in callers):
    raise ValueError("callers must be a list of application ids.")
  elif callers and domain.type == "public":
    raise ValueError("A public domain takes no allowed applications.")
  if callers is None:
    return False
  wanted = set(callers)
  if unknown := wanted - {application.id for application in Application.select(Application.id).where(Application.id.in_(wanted))}:
    raise ValueError(f"Unknown applications: {', '.join(str(application_id) for application_id in sorted(unknown))}.")
  if wanted == {caller.application_id for caller in domain.callers}:
    return False
  DomainCaller.delete().where(DomainCaller.domain == domain.id).execute()
  DomainCaller.insert_many([{"domain": domain.id, "application": application_id} for application_id in sorted(wanted)]).execute()
  return True


def create_domain(container_id: str | None, fields: dict) -> Domain:
  if container_id is None:
    raise ValueError("A domain without a container is not allowed.")
  with write():
    container = get(Container, Container.id == container_id)
    draft, callers = Domain(), fields.get("callers")
    fields = {key: value for key, value in fields.items() if key != "callers"}
    config = {**merged(draft, fields, DOMAIN_FIELDS), "container_id": container.id, "namespace_worker_id": None}
    if config["name"] is None:
      raise ValueError("name is required.")
    _settle(config, set(DOMAIN_FIELDS), draft.id, container.worker_id)
    domain = Domain.create(id=draft.id, **config)
    _write_callers(domain, callers)
  domain_changed(domain)
  return domain


def patch_domain(domain_id: str, fields: dict) -> Domain:
  """Changes what is sent; its container stays."""
  with write():
    domain = get(Domain, Domain.id == domain_id)
    callers = fields.get("callers")
    fields = {key: value for key, value in fields.items() if key != "callers"}
    config = {**merged(domain, fields, DOMAIN_FIELDS), "namespace_worker_id": domain.namespace_worker_id}
    _settle(config, set(fields), domain.id, domain.container.worker_id)
    changed = save_changes(domain, config)
    callers_changed = _write_callers(domain, callers)
  if changed or callers_changed:
    domain_changed(domain)
  return domain


def delete_domain(domain_id: str):
  with write():
    domain = get(Domain, Domain.id == domain_id)
    domain.delete_instance()
  domain_changed(domain)
