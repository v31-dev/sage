import hashlib
import json
import re
from dataclasses import asdict, dataclass

from services.db.models import Domain
from utils.common import OPAQUE_ID
from values.names import qualified_name

ROUTING_TEMPLATES = {
    "public": "service_public.yml",
    "internal": "service_internal.yml",
    "tcp": "service_internal_tcp.yml",
}
ARTIFACTS = {"file": ("domain-", ".yml"), "dns": ("sage-domain-", "")}
# The first line of a routing file, naming the domain and the fingerprint it was rendered from.
ROUTE_HEADER = "# sage-domain"
RESERVED_DOMAIN_NAMES = {"core", "int", "discovery"}


def domain_artifact_name(kind: str, domain_id: str) -> str:
  prefix, suffix = ARTIFACTS[kind]
  return f"{prefix}{domain_id}{suffix}"


def domain_artifact_owner(kind: str, value: str) -> str | None:
  prefix, suffix = ARTIFACTS[kind]
  match = re.fullmatch(rf"{re.escape(prefix)}({OPAQUE_ID}){re.escape(suffix)}", value)
  return match[1] if match else None


def routes_command(dynamic: str) -> str:
  """Lists the first line of every routing file in the worker's dynamic directory for `parse_route_listing`."""
  return f"grep -H -m1 '' {dynamic}/{ARTIFACTS['file'][0]}*{ARTIFACTS['file'][1]} 2>/dev/null || true"


def parse_route_listing(lines: list[str]) -> dict[str, str | None]:
  """Each routing file's domain id and the fingerprint its header names; None for a file whose first line is not its
  own domain's header."""
  routes = {}
  for line in lines:
    path, separator, first = line.partition(":")
    domain_id = domain_artifact_owner("file", path.rsplit("/", 1)[-1]) if separator else None
    if domain_id is None:
      continue
    match = re.fullmatch(rf"{re.escape(ROUTE_HEADER)} ({OPAQUE_ID}) ([0-9a-f]{{64}})", first)
    routes[domain_id] = match[2] if match and match[1] == domain_id else None
  return routes


def validate_port(port) -> int:
  if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
    raise ValueError(f"Invalid port {port!r}: must be a number from 1 to 65535.")
  return port


def host(domain: Domain, base_domain: str) -> str | None:
  """The domain's full name; none for an internal or TCP one without a namespace worker."""
  if domain.type == "public":
    return f"{domain.name}.{base_domain}"
  return f"{domain.name}.{domain.namespace_worker.subdomain}.int.{base_domain}" if domain.namespace_worker_id else None


def address(domain: Domain, base_domain: str) -> str | None:
  """How a client reaches it: an https URL, or `host:8443` for TCP."""
  name = host(domain, base_domain)
  if name is None:
    return None
  return f"{name}:8443" if domain.type == "tcp" else f"https://{name}"


def allowed_origins(domain: Domain, base_domain: str) -> tuple[str, ...]:
  """Its explicit CORS origins, or its own address when it has none; none for TCP."""
  if domain.type == "tcp" or host(domain, base_domain) is None:
    return ()
  return tuple(sorted(domain.cors_origins or [address(domain, base_domain)]))


@dataclass(frozen=True)
class DomainConfig:
  domain_id: str
  type: str
  host: str | None
  port: int
  worker: str | None
  container_id: str | None
  application_id: int | None
  application: str | None
  cors_origins: tuple[str, ...]
  templates: str

  @classmethod
  def capture(cls, domain: Domain, base_domain: str, templates: str):
    container = domain.container
    application = container.application if container else None
    return cls(
        domain_id=domain.id, type=domain.type, host=host(domain, base_domain), port=domain.port,
        worker=container.worker_id if container else domain.namespace_worker_id,
        container_id=domain.container_id, application_id=application.id if application else None,
        application=qualified_name(application.project_id, application.name) if application else None,
        cors_origins=allowed_origins(domain, base_domain), templates=templates,
    )

  @property
  def content_hash(self) -> str:
    return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

  @property
  def filename(self) -> str:
    return domain_artifact_name("file", self.domain_id)

  @property
  def dns_comment(self) -> str:
    return domain_artifact_name("dns", self.domain_id)

  def dns_records(self, tunnel: dict | None) -> dict[str, str]:
    if self.type != "public" or self.container_id is None:
      return {}
    if tunnel is None:
      raise ValueError(f"Worker {self.worker} has no tunnel.")
    return {self.host: f"{tunnel['id']}.cfargotunnel.com"}
