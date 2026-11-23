import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from services.applications.deploy_input import DeployConfig
from services.containers import rendering
from services.containers.placement import LABELS
from services.containers.record import Authority
from services.containers.rendering import deployment_hash, render_placement

RENDERED = Path(__file__).parent / "rendered"
# Ids that YAML 1.2, which Compose reads, takes for numbers unless they are quoted.
AUTHORITY = Authority("1234567e89012345", "0000000000000123", "2222222222222222")
DEPLOYMENT = "5555555555555e55"

DOCKER = DeployConfig(
    qualified_name="shop-web", type="docker", image="nginx:1.27", repo=None, path="Dockerfile",
    env="DB=${DATABASE_URL}\nHOST=${SAGE_WORKER_HOSTNAME}\nKEEP=${UNKNOWN}", args="", build_secrets="",
    command='python -m app --name "a b" ${PORT}', project_env="DATABASE_URL=postgres://db\nPORT=8080",
    volumes=(("3333333333333333", "/data"), ("4444444444444444", "/var/cache")))
GIT = DeployConfig(
    qualified_name="shop-api", type="git", image=None, repo="https://github.com/user/api.git#main",
    path="docker/Dockerfile", env="TOKEN=${TOKEN}", args='VERSION=${VERSION}\nQUOTED="x y"',
    build_secrets="NPM_TOKEN=${TOKEN}\nOTHER='s3cr3t'", command="", project_env="TOKEN=abc\nVERSION=1.2", volumes=())


def render(deploy):
  return {file.path: (file.content, file.mode) for file in render_placement(deploy, AUTHORITY, DEPLOYMENT, "w1", "/opt/sage")}


def test_a_deployments_hash_follows_its_inputs_and_the_compose_templates(monkeypatch):
  before = deployment_hash(DOCKER)
  assert re.fullmatch("[0-9a-f]{64}", before)
  assert deployment_hash(dataclasses.replace(DOCKER, qualified_name="other-name")) == before
  assert deployment_hash(dataclasses.replace(DOCKER, image="nginx:1.28")) != before
  monkeypatch.setattr(rendering, "COMPOSE_DIGEST", "changed")
  assert deployment_hash(DOCKER) != before


def test_a_docker_deployment_is_its_env_file_and_its_compose_file():
  files = render(DOCKER)
  assert list(files) == [".env", "docker-compose.yml"]
  assert files[".env"] == ("DB=postgres://db\nHOST=w1\nKEEP=${UNKNOWN}", 0o600)
  assert files["docker-compose.yml"] == ((RENDERED / "docker.yml").read_text(), 0o644)


def test_a_git_deployment_has_a_private_file_for_each_build_secret():
  files = render(GIT)
  assert list(files) == [".env", "secrets/NPM_TOKEN", "secrets/OTHER", "docker-compose.yml"]
  assert (files[".env"], files["secrets/NPM_TOKEN"], files["secrets/OTHER"]) == (
      ("TOKEN=abc", 0o600), ("abc", 0o600), ("s3cr3t", 0o600))
  assert files["docker-compose.yml"] == ((RENDERED / "git.yml").read_text(), 0o644)


def test_a_blank_command_leaves_the_images_own_and_a_command_is_passed_token_by_token():
  def command(deploy):
    [service] = yaml.safe_load(render(deploy)["docker-compose.yml"][0])["services"].values()
    return service["command"]

  assert command(GIT) is None
  assert command(DOCKER) == ["python", "-m", "app", "--name", "a b", "8080"]


@pytest.mark.parametrize("deploy", [DOCKER, GIT], ids=["docker", "git"])
def test_every_label_value_stays_a_quoted_string(deploy):
  lines = render(deploy)["docker-compose.yml"][0].splitlines()
  values = {"sage.instance": AUTHORITY.instance_id, "sage.generation": AUTHORITY.state_generation,
            "sage.container": AUTHORITY.container_id, "sage.deployment": DEPLOYMENT,
            "sage.application": deploy.qualified_name}
  assert set(values) == set(LABELS)
  for label, value in values.items():
    assert f'      {label}: "{value}"' in lines, label


@pytest.mark.parametrize("deploy", [DOCKER, GIT], ids=["docker", "git"])
def test_a_placement_has_a_network_of_its_own_named_and_labelled_by_its_container(deploy):
  compose = yaml.safe_load(render(deploy)["docker-compose.yml"][0])
  (service,) = compose["services"].values()
  assert service["networks"] == ["default"]
  assert compose["networks"] == {"default": {
      "name": "sage-2222222222222222", "driver_opts": {"com.docker.network.bridge.name": "sage22222222222"},
      "labels": {"sage.container": "2222222222222222"}}}


def test_inputs_that_break_the_compose_file_are_refused():
  with pytest.raises(yaml.YAMLError):
    render_placement(DeployConfig(**{**DOCKER.__dict__, "type": "git", "path": "a: b"}), AUTHORITY, DEPLOYMENT, "w1",
                     "/opt/sage")
