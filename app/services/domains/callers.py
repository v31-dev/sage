"""A Domain's callers: the containers of the applications an internal or TCP Domain allows, and what lets them reach
its name. Traefik on the caller's own network tells the application by its network's subnet, from the Domain's file on
the caller's worker: on the Domain's own worker beside its route, to its own service; on another, passing the
connection through to the Domain's worker's tailnet address, where its route admits it as the tailnet. The names each
caller may reach are aliases of Traefik on its network."""
import hashlib
import json
from dataclasses import dataclass

import yaml

from services.db.models import Container, Domain, DomainCaller
from services.domains.config import DomainConfig, host

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


def routed(home: str, worker: str, address: str | None) -> bool:
  """Whether `worker` routes the callers of a name whose route is on `home`: it is `home`, or `home`'s address is known
  to pass them through to."""
  return worker == home or bool(address)


def caller_aliases(names: tuple[CalledName, ...], worker: str, applications: dict[str, int],
                   addresses: dict[str, str | None]) -> dict[str, tuple[str, ...]]:
  """The names Traefik answers to on each application's network on `worker`, given the application of each network:
  those its application may call that `worker` routes, so a lookup there finds Traefik on that network."""
  return {network: tuple(sorted(name.host for name in names if application in name.callers
                                and routed(name.worker, worker, addresses.get(name.worker))))
          for network, application in applications.items()}


def worker_fingerprints(config: DomainConfig, networks: dict[str, dict[str, tuple[str, ...]] | None],
                        address: str | None) -> dict[str, str | None]:
  """The fingerprint the Domain's file should carry on each worker it has one on, by hostname: its own worker and each
  with a container of an allowed application. On its own worker with no caller there it is the configuration's own;
  otherwise it also covers the callers' subnets there, from `networks` (each worker's, by container id, None until
  listed) and, on another worker, `address`, its own worker's tailnet address. None while those aren't known."""
  fingerprints = {}
  for worker in config.workers:
    here = {container_id for host, container_id, _ in config.callers if host == worker}
    listed, remote = networks.get(worker), worker != config.worker
    if not remote and not here:
      fingerprints[worker] = config.content_hash
    elif listed is None or not routed(config.worker, worker, address):
      fingerprints[worker] = None
    else:
      subnets = sorted({subnet for container_id in here for subnet in listed.get(container_id, ())})
      fingerprints[worker] = hashlib.sha256(
          json.dumps([config.content_hash, subnets, address if remote else None]).encode()).hexdigest()
  return fingerprints


def domain_file(config: DomainConfig, worker: str, networks: dict[str, tuple[str, ...]],
                address: str | None) -> tuple[str, str] | None:
  """The Domain's `domain-<id>.yml` on `worker`, given its applications' networks there (subnets by container id) and
  the Domain's worker's address, with the fingerprint its header carries: on its own worker its route, with routers
  for the callers there; on another, routers passing those callers' connections through to its worker. Routers let
  through only the allowed containers' subnets, so a file with none has no router. None while the address a remote
  file needs isn't known."""
  fingerprint = worker_fingerprints(config, {worker: networks}, address)[worker]
  if fingerprint is None:
    return None
  here = {container_id for host, container_id, _ in config.callers if host == worker}
  if worker == config.worker and not here:
    return fingerprint, config.route()
  allowed = sorted({subnet for container_id in here for subnet in networks.get(container_id, ())})
  router = f"domain_{config.domain_id}_callers"
  callers = " || ".join(f"ClientIP(`{subnet}`)" for subnet in allowed)
  entry_point, port = ENTRY_POINTS[config.type]
  body = yaml.safe_load(config.route()) if worker == config.worker else {}
  if allowed and worker == config.worker and config.type == "internal":
    body["http"]["routers"][router] = {
        "rule": f"Host(`{config.host}`) && ({callers})", "entryPoints": [entry_point],
        "middlewares": [f"domain_{config.domain_id}_headers", f"domain_{config.domain_id}_cors"],
        "service": f"domain_{config.domain_id}", "tls": {}}
  elif allowed and worker == config.worker:
    body["tcp"]["routers"][router] = {
        "rule": f"HostSNI(`{config.host}`) && ({callers})", "entryPoints": [entry_point],
        "service": f"domain_{config.domain_id}", "tls": {"options": "backend-tcp"}}
  elif allowed:
    body["tcp"] = {
        "routers": {router: {"rule": f"HostSNI(`{config.host}`) && ({callers})", "entryPoints": [entry_point],
                             "service": router, "tls": {"passthrough": True}}},
        "services": {router: {"loadBalancer": {"servers": [{"address": f"{address}:{port}"}]}}}}
  return fingerprint, f"# sage-domain {config.domain_id} {fingerprint}\n{yaml.safe_dump(body, sort_keys=True)}"


def allowing(application_id: int) -> list[str]:
  """The ids of the Domains that allow the application."""
  return [caller.domain_id for caller in DomainCaller.select(DomainCaller.domain).where(DomainCaller.application == application_id)]
