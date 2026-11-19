"""The worker's stack as Sage writes it: its files rendered from their inputs, and the fingerprint the worker's infra
stamp holds once they are applied."""
import hashlib
import json
from dataclasses import asdict, dataclass

import config
from utils.common import fill


def worker_scope(hostname: str) -> str:
  """The queue's scope of the worker, over all it holds: its containers, stack, routes, discovery and certificate files."""
  return f"worker:{hostname}"


def stack_scope(hostname: str) -> str:
  return f"{worker_scope(hostname)}:stack"


# The network of the stack's tunnels and Traefik, named in the Compose template.
EDGE_NETWORK = "sage_edge"
# The line `edge_network_command` prints when it had to create the network.
EDGE_NETWORK_CREATED = "created"


def edge_network_command() -> str:
  """Creates the edge network when it is missing, saying `EDGE_NETWORK_CREATED` then, and lists its subnets, one a line,
  for `decisions.edge_subnets`."""
  return (f"docker network inspect {EDGE_NETWORK} >/dev/null 2>&1 || "
          f"{{ docker network create {EDGE_NETWORK} >/dev/null && echo {EDGE_NETWORK_CREATED}; }}; "
          f"docker network inspect -f '{{{{range .IPAM.Config}}}}{{{{println .Subnet}}}}{{{{end}}}}' {EDGE_NETWORK}")


def remove_edge_network_command() -> str:
  """Removes the edge network if it is there."""
  return f"(! docker network inspect {EDGE_NETWORK} >/dev/null 2>&1 || docker network rm {EDGE_NETWORK} >/dev/null)"


# The folders of the stack's Traefik that only Sage writes into, under the worker's home.
TRAEFIK_FOLDERS = ("traefik/dynamic", "traefik/certs")


def traefik_files_command(home: str) -> str:
  """Lists every entry of `TRAEFIK_FOLDERS`, as `<folder>/<name>`, for `decisions.stray_traefik_files`."""
  return f"cd {home} && find {' '.join(TRAEFIK_FOLDERS)} -mindepth 1 -maxdepth 1 2>/dev/null; true"


# The templates the stack's files come from, under the worker templates.
INFRA_TEMPLATES = ("docker-compose.yml", "worker.env", "traefik/traefik.yml", "traefik/config.yml", "vector/vector.yml",
                   "telegraf/telegraf.conf")


@dataclass(frozen=True)
class InfraInputs:
  """What the stack's files are rendered from, apart from the tunnel tokens and the Docker socket's group, which never
  change without one of these."""
  hostname: str
  address: str
  manager_address: str
  domain: str
  platform_tunnel: str
  worker_tunnel: str
  version: str
  report_interval: int
  edge_subnets: tuple[str, ...]


@dataclass(frozen=True)
class StackFile:
  path: str  # relative to the worker's home
  content: str
  mode: int


def applications_memory_mb(total_mb: int) -> int:
  """What the application containers on a worker with `total_mb` of memory may use together."""
  return total_mb - config.WORKER_MEMORY_RESERVE_MB


def infra_fingerprint(inputs: InfraInputs) -> str:
  """The fingerprint of everything the stack's files are rendered from, the templates included."""
  digest = hashlib.sha256(json.dumps(asdict(inputs), sort_keys=True).encode())
  for name in INFRA_TEMPLATES:
    digest.update(name.encode())
    digest.update((config.TEMPLATES_DIR / "worker" / name).read_bytes())
  return digest.hexdigest()[:16]


def render_infrastructure(inputs: InfraInputs, tunnel_token: str, discovery_tunnel_token: str, docker_gid: str,
                          home: str) -> tuple[StackFile, ...]:
  """The stack's files. Its Compose file is written as it is: its `${...}` are Compose's own, read from the `.env`
  beside it on the worker."""
  def template(name: str) -> str:
    return (config.TEMPLATES_DIR / "worker" / name).read_text()

  return (
      StackFile("docker-compose.yml", template("docker-compose.yml"), 0o644),
      StackFile(".env", fill(template("worker.env"), {
          "SAGE_HOME": home, "TS_IP": inputs.address, "TS_HOSTNAME": inputs.hostname, "TUNNEL_TOKEN": tunnel_token,
          "DISCOVERY_TUNNEL_TOKEN": discovery_tunnel_token, "DOCKER_GID": docker_gid}), 0o600),
      StackFile("traefik/traefik.yml", fill(template("traefik/traefik.yml"),
                                            {"EDGE_SUBNETS": json.dumps(list(inputs.edge_subnets))}), 0o644),
      StackFile("traefik/dynamic/config.yml", template("traefik/config.yml"), 0o644),
      StackFile("vector/config/vector.yml", fill(template("vector/vector.yml"),
                                                 {"IP": inputs.manager_address, "INGEST_PORT": config.INGEST_PORT,
                                                  "HOSTNAME": inputs.hostname}), 0o644),
      StackFile("telegraf/telegraf.conf", fill(template("telegraf/telegraf.conf"), {
          "IP": inputs.manager_address, "INGEST_PORT": config.INGEST_PORT, "HOSTNAME": inputs.hostname,
          "TS_IP": inputs.address, "INTERVAL": inputs.report_interval}), 0o644),
  )
