"""The worker's stack as Sage writes it: its files rendered from their inputs, and the fingerprint the worker's infra
stamp holds once they are applied."""
import hashlib
import ipaddress
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


# What every application bridge's interface name starts with, and no other interface's: the firewall matches them by it.
APPLICATION_BRIDGE_PREFIX = "sage"
# What the application bridges may not reach: the tailnet, other private networks and link-local addresses (cloud
# metadata); `fc00::/7` holds the tailnet's IPv6 range.
PRIVATE_RANGES = ("100.64.0.0/10", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7",
                  "fe80::/10")
FIREWALL_TABLE = "sage"
FIREWALL_UNIT = "sage-firewall.service"
# Where the unit is installed: systemd reads it from there before any other filesystem is mounted.
FIREWALL_UNIT_PATH = f"/etc/systemd/system/{FIREWALL_UNIT}"
# The stack's container with no `dns` of its own, whose `resolv.conf` records the resolvers Docker gives an application.
DEFAULT_DNS_CONTAINER = "cloudflared"
TRAEFIK_CONTAINER = "traefik"
# The firewall's files under the worker's home, and the templates they come from under the worker templates.
FIREWALL_FILES = ("firewall/sage.nft", f"firewall/{FIREWALL_UNIT}")


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


def render_firewall(resolvers: tuple[str, ...], home: str) -> tuple[StackFile, ...]:
  """The firewall's table and the boot unit that loads it, with `resolvers` the private resolvers DNS may reach
  (`decisions.dns_exceptions`)."""
  def elements(version: int) -> str:
    found = [address for address in resolvers if ipaddress.ip_address(address).version == version]
    return f"elements = {{ {', '.join(found)} }}" if found else ""

  table, unit = FIREWALL_FILES
  ranges = {version: ", ".join(network for network in PRIVATE_RANGES if ipaddress.ip_network(network).version == version)
            for version in (4, 6)}
  return (
      StackFile(table, fill((config.TEMPLATES_DIR / "worker" / table).read_text(), {
          "TABLE": FIREWALL_TABLE, "BRIDGES": f"{APPLICATION_BRIDGE_PREFIX}*", "PRIVATE4": ranges[4],
          "PRIVATE6": ranges[6], "RESOLVERS4": elements(4), "RESOLVERS6": elements(6)}), 0o644),
      StackFile(unit, fill((config.TEMPLATES_DIR / "worker" / unit).read_text(),
                           {"TABLE": FIREWALL_TABLE, "TABLE_FILE": f"{home}/{table}"}), 0o644),
  )


def firewall_fingerprint(files: tuple[StackFile, ...]) -> str:
  """The fingerprint of the firewall's rendered files, which the worker's firewall stamp holds once they are applied."""
  digest = hashlib.sha256()
  for file in files:
    digest.update(file.path.encode())
    digest.update(file.content.encode())
  return digest.hexdigest()[:16]
