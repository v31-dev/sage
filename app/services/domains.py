import hashlib
import json
import re
from dataclasses import asdict, dataclass

from services.db import Domain
from services.updates import Updates, UpdateSource

ROUTING_TEMPLATES = {
    "public": "service_public.yml",
    "internal": "service_internal.yml",
    "tcp": "service_internal_tcp.yml",
}
ARTIFACTS = {"file": ("domain-", ".yml"), "dns": ("sage-domain-", "")}


def domain_artifact_name(kind: str, domain_id: int) -> str:
  prefix, suffix = ARTIFACTS[kind]
  return f"{prefix}{domain_id}{suffix}"


def domain_artifact_owner(kind: str, value: str) -> int | None:
  prefix, suffix = ARTIFACTS[kind]
  match = re.fullmatch(rf"{re.escape(prefix)}([1-9][0-9]*){re.escape(suffix)}", value)
  return int(match[1]) if match else None


def domains_changed():
  Updates().changed(UpdateSource.DOMAINS)


@dataclass(frozen=True)
class DomainConfig:
  domain_id: int
  type: str
  host: str | None
  port: int
  worker: str | None
  container_id: int | None
  application_id: int | None
  application: str | None
  active: bool
  templates: str

  @classmethod
  def capture(cls, domain: Domain, base_domain: str, templates: str):
    container = domain.container
    return cls(
        domain_id=domain.id, type=domain.type, host=domain.host(base_domain), port=domain.port,
        worker=container.worker_id if container else domain.worker_id,
        container_id=domain.container_id, application_id=container.application_id if container else None,
        application=container.application.qualified_name if container else None,
        active=container is not None and container.status == "active", templates=templates,
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
