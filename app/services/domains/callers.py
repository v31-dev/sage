"""The callers' file: on each worker, beside the Domains' routes, what lets the containers of an application that an
internal or TCP Domain allows reach that Domain's name. Traefik on the caller's own network tells the application by
its network's subnet; a name routed on this worker goes to the Domain's own service, one on another worker passes
through to that worker's tailnet address, where the Domain's route admits it as the tailnet."""
import hashlib
from dataclasses import dataclass

import yaml

from services.db.models import Container, Domain, DomainCaller
from services.domains.config import host

CALLERS_FILE = "_callers.yml"
# Its first line: this marker, then the fingerprint of the rest.
MARKER = "# sage-callers"
# Where an internal or TCP name is served on its worker's tailnet address.
ENTRY_POINTS = {"internal": ("websecure", 443), "tcp": ("tcps", 8443)}


@dataclass(frozen=True)
class CalledName:
  """An internal or TCP Domain that allows callers: its full name, the worker its route is on and the applications
  allowed."""
  domain_id: str
  type: str
  host: str
  worker: str
  callers: frozenset[int]


def called_names(base_domain: str) -> tuple[CalledName, ...]:
  """Every internal or TCP Domain with a name that allows at least one application, by id."""
  callers = {}
  for caller in DomainCaller.select(DomainCaller.domain, DomainCaller.application):
    callers.setdefault(caller.domain_id, set()).add(caller.application_id)
  domains = Domain.select(Domain, Container).join(Container).where(Domain.id.in_(list(callers))).order_by(Domain.id)
  return tuple(CalledName(domain.id, domain.type, name, domain.container.worker_id, frozenset(callers[domain.id]))
               for domain in domains if domain.type in ENTRY_POINTS and (name := host(domain, base_domain)))


def routed(name: CalledName, worker: str, addresses: dict[str, str | None]) -> bool:
  """Whether `worker` routes `name`'s callers: on its own worker, or through to one whose address is known."""
  return name.worker == worker or bool(addresses.get(name.worker))


def caller_aliases(names: tuple[CalledName, ...], worker: str, applications: dict[str, int],
                   addresses: dict[str, str | None]) -> dict[str, tuple[str, ...]]:
  """The names Traefik answers to on each application's network on `worker`, given the application of each network:
  those its application may call that `worker` routes, so a lookup there finds Traefik on that network."""
  return {network: tuple(sorted(name.host for name in names if application in name.callers and routed(name, worker, addresses)))
          for network, application in applications.items()}


def render_callers(names: tuple[CalledName, ...], worker: str, subnets: dict[int, tuple[str, ...]],
                   addresses: dict[str, str | None]) -> str | None:
  """Worker `worker`'s callers' file, from the names that allow callers, the subnets of the applications' networks on
  it by application id, and the workers' tailnet addresses; None when no name has a caller here. A name on a worker
  whose address isn't known is left out until it is."""
  http_routers, tcp_routers, tcp_services = {}, {}, {}
  for name in names:
    allowed = sorted({subnet for application in name.callers for subnet in subnets.get(application, ())})
    if not allowed or not routed(name, worker, addresses):
      continue
    callers = " || ".join(f"ClientIP(`{subnet}`)" for subnet in allowed)
    router, (entry_point, port) = f"caller_{name.domain_id}", ENTRY_POINTS[name.type]
    if name.worker == worker and name.type == "internal":
      http_routers[router] = {"rule": f"Host(`{name.host}`) && ({callers})", "entryPoints": [entry_point],
                              "middlewares": [f"domain_{name.domain_id}_headers@file", f"domain_{name.domain_id}_cors@file"],
                              "service": f"domain_{name.domain_id}@file", "tls": {}}
    elif name.worker == worker:
      tcp_routers[router] = {"rule": f"HostSNI(`{name.host}`) && ({callers})", "entryPoints": [entry_point],
                             "service": f"domain_{name.domain_id}@file", "tls": {"options": "backend-tcp"}}
    else:
      tcp_routers[router] = {"rule": f"HostSNI(`{name.host}`) && ({callers})", "entryPoints": [entry_point],
                             "service": router, "tls": {"passthrough": True}}
      tcp_services[router] = {"loadBalancer": {"servers": [{"address": f"{addresses[name.worker]}:{port}"}]}}
  config = {}
  if http_routers:
    config["http"] = {"routers": http_routers}
  if tcp_routers:
    config["tcp"] = {"routers": tcp_routers, **({"services": tcp_services} if tcp_services else {})}
  if not config:
    return None
  body = yaml.safe_dump(config, sort_keys=True)
  return f"{MARKER} {hashlib.sha256(body.encode()).hexdigest()[:16]}\n{body}"
