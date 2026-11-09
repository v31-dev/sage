"""The files a deployment writes into its placement, rendered from its frozen deploy inputs."""
import json
import shlex
from dataclasses import dataclass
from string import Template

import yaml

import config
from services.applications.deploy_input import DeployConfig
from services.containers.placement import runtime_name, volume_dir
from services.containers.record import Authority
from values.key_values import parse_multiline_kv

COMPOSE_TEMPLATES = {"docker": "dockerhub-compose.yml", "git": "gitrepo-compose.yml"}


@dataclass(frozen=True)
class PlacementFile:
  path: str  # relative to the placement directory
  content: str
  mode: int


def render_placement(deploy: DeployConfig, authority: Authority, deployment_id: str, worker: str,
                     home: str) -> tuple[PlacementFile, ...]:
  """The env file, each build secret (a Docker application has none) and the Compose file. The project's env, with
  SAGE_WORKER_HOSTNAME, fills the `${KEY}` references in the env, build args, build secrets and command."""
  project_env = [*parse_multiline_kv(deploy.project_env, lambda key, value: (key, value)), ("SAGE_WORKER_HOSTNAME", worker)]
  env, args, build_secrets, command = deploy.env, deploy.args, deploy.build_secrets, deploy.command
  for key, value in project_env:
    env, args, build_secrets, command = (text.replace("${" + key + "}", value) for text in (env, args, build_secrets, command))
  secrets = parse_multiline_kv(build_secrets, lambda key, value: (key, value), strip_quotes=True)

  template = (config.TEMPLATES_DIR / "worker/application" / COMPOSE_TEMPLATES[deploy.type]).read_text()
  compose = Template(template).substitute(
      RUNTIME_NAME=runtime_name(authority.container_id),
      INSTANCE_ID=authority.instance_id,
      STATE_GENERATION=authority.state_generation,
      CONTAINER_ID=authority.container_id,
      DEPLOYMENT_ID=deployment_id,
      APPLICATION=deploy.qualified_name,
      IMAGE=deploy.image or "",
      REPO=deploy.repo or "",
      DOCKERFILE=deploy.path,
      BUILD_ARGS=", ".join(parse_multiline_kv(args, lambda key, value: json.dumps(f"{key}={value}"), strip_quotes=True)),
      BUILD_SECRETS=", ".join(name for name, _ in secrets),
      SECRETS_BLOCK="secrets:\n" + "\n".join(f"  {name}:\n    file: ./secrets/{name}" for name, _ in secrets) if secrets else "",
      # Exec form passes each token literally. Blank leaves `command:` null, so the image's CMD runs; an empty
      # string would replace it with nothing.
      COMMAND=f"[{', '.join(json.dumps(token) for token in shlex.split(command))}]" if command.strip() else "",
      VOLUMES=", ".join(json.dumps(f"{volume_dir(home, authority.container_id, volume_id)}:{path}")
                        for volume_id, path in deploy.volumes),
  )
  # Parsed only to refuse a malformed file: dumping it again would drop the template's quotes, and Compose reads YAML
  # 1.2, where an unquoted id such as 5555555555555e55 is a number.
  yaml.safe_load(compose)
  return (PlacementFile(".env", env, 0o600),
          *(PlacementFile(f"secrets/{name}", value, 0o600) for name, value in secrets),
          PlacementFile("docker-compose.yml", compose, 0o644))
