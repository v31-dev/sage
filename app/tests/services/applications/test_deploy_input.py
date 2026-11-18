import dataclasses

import pytest

from services.applications.commands import create_volume, patch_application, patch_project, patch_volume
from services.applications.deploy_input import DeployConfig, capture_deploy_config

DEPLOY_CONFIG = DeployConfig(
    qualified_name="demo-web", type="docker", image="nginx:1.27", repo=None, path="Dockerfile", env="A=1", args="",
    build_secrets="", command="", project_env="P=1", volumes=(("0123456789abcdef", "/data"),))


def test_the_hash_is_pinned_because_a_change_marks_every_deployed_application_as_changed():
  assert DEPLOY_CONFIG.content_hash == "bbaaf3fab173edcde32675757f63d6bdd7ae45df99d9a3da3753a6e85780ba73"


def test_the_name_is_not_part_of_it():
  assert dataclasses.replace(DEPLOY_CONFIG, qualified_name="other-name").content_hash == DEPLOY_CONFIG.content_hash


@pytest.mark.parametrize("field, value", [
    ("type", "git"),
    ("image", "nginx:1.28"),
    ("repo", "https://github.com/user/repo.git"),
    ("path", "docker/Dockerfile"),
    ("env", "A=2"),
    ("args", "ARG=1"),
    ("build_secrets", "SECRET=1"),
    ("command", "serve"),
    ("project_env", "P=2"),
    ("volumes", (("0123456789abcdef", "/other"),)),
    ("volumes", (("fedcba9876543210", "/data"),)),
    ("volumes", ()),
])
def test_every_deploy_input_is_part_of_it(field, value):
  assert dataclasses.replace(DEPLOY_CONFIG, **{field: value}).content_hash != DEPLOY_CONFIG.content_hash


def test_it_is_a_snapshot_that_cannot_be_changed():
  with pytest.raises(dataclasses.FrozenInstanceError):
    DEPLOY_CONFIG.env = "A=2"


def test_it_holds_the_current_deploy_inputs_with_unset_text_as_empty(application):
  patch_application(application.id, {"env": "A=1", "command": "serve"})
  patch_project("demo", {"env": "P=1"})
  second = create_volume(application.id, "zeta", "/z")
  first = create_volume(application.id, "alpha", "/a")

  assert capture_deploy_config(application.id) == DeployConfig(
      qualified_name="demo-web", type="docker", image="nginx", repo=None, path="Dockerfile", env="A=1", args="",
      build_secrets="", command="serve", project_env="P=1",
      volumes=tuple(sorted([(first.id, "/a"), (second.id, "/z")])))


def test_renaming_a_volume_leaves_the_hash_and_moving_its_mount_changes_it(application):
  volume = create_volume(application.id, "data", "/data")
  before = capture_deploy_config(application.id).content_hash

  patch_volume(volume.id, {"name": "files"})
  assert capture_deploy_config(application.id).content_hash == before

  patch_volume(volume.id, {"path": "/files"})
  assert capture_deploy_config(application.id).content_hash != before


def test_a_snapshot_keeps_its_inputs_after_a_later_edit(application):
  snapshot = capture_deploy_config(application.id)

  patch_application(application.id, {"image": "nginx:1.28", "env": "A=2"})

  assert (snapshot.image, snapshot.env) == ("nginx", "")
  assert capture_deploy_config(application.id).image == "nginx:1.28"


def test_a_missing_application_is_not_found(db):
  with pytest.raises(LookupError, match="Application not found"):
    capture_deploy_config(1)
