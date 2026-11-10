"""The worker's stack as Sage writes it: its files rendered from their inputs, and the fingerprint the worker's infra
stamp holds once they are applied."""
import hashlib
import json
from dataclasses import asdict, dataclass

import config
from utils.common import fill

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


@dataclass(frozen=True)
class StackFile:
  path: str  # relative to the worker's home
  content: str
  mode: int


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
      StackFile("traefik/traefik.yml", template("traefik/traefik.yml"), 0o644),
      StackFile("traefik/dynamic/config.yml", fill(template("traefik/config.yml"),
                                                   {"DOMAIN": inputs.domain, "HOSTNAME": inputs.hostname}), 0o644),
      StackFile("vector/config/vector.yml", fill(template("vector/vector.yml"),
                                                 {"IP": inputs.manager_address, "HOSTNAME": inputs.hostname}), 0o644),
      StackFile("telegraf/telegraf.conf", fill(template("telegraf/telegraf.conf"), {
          "IP": inputs.manager_address, "HOSTNAME": inputs.hostname, "TS_IP": inputs.address,
          "INTERVAL": inputs.report_interval}), 0o644),
  )
