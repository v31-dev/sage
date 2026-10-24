import dataclasses
from datetime import datetime

import pytest
from peewee import IntegrityError

from services.db import Application, Backup, Container, DeployConfig, Domain, Event, Project, Task, Volume, Worker
from utils.common import utcnow

PAST = datetime(2000, 1, 2, 3, 4, 5)

DEPLOY_CONFIG = DeployConfig(
    qualified_name="demo-web", deploy_stamp="2026-10-23T10:05:07.123456", type="docker", image="nginx:1.27", repo=None, path="Dockerfile",
    env="A=1", args="", build_secrets="", command="", project_env="P=1", volumes=(("data", "/data"),))


class TestBaseModel:
  def test_both_timestamps_are_set_when_a_row_is_created(self, db):
    before = utcnow()
    project = Project.create(name="demo", label="Demo")
    after = utcnow()

    assert before <= project.created_at <= after
    assert before <= project.updated_at <= after

  def test_saving_moves_updated_at_forward_and_leaves_created_at(self, project):
    Project.update(created_at=PAST, updated_at=PAST).execute()
    stored = Project.get_by_id("demo")
    before = utcnow()

    stored.label = "Renamed"
    stored.save()

    saved = Project.get_by_id("demo")
    assert saved.created_at == PAST
    assert saved.updated_at >= before

  def test_only_the_changed_fields_are_written(self, project):
    first, second = Project.get_by_id("demo"), Project.get_by_id("demo")

    first.label = "From the first"
    first.save()
    second.description = "From the second"
    second.save()

    saved = Project.get_by_id("demo")
    assert (saved.label, saved.description) == ("From the first", "From the second")


class TestProject:
  def test_a_project_needs_a_label(self, db):
    with pytest.raises(IntegrityError):
      Project.create(name="demo")

  def test_the_name_is_unique(self, project):
    with pytest.raises(IntegrityError):
      Project.create(name="demo", label="Another")

  @pytest.mark.parametrize("name", ["Demo", "my-project", "1abc", "has space", ""])
  def test_a_name_must_be_lowercase_letters_and_digits_starting_with_a_letter(self, db, name):
    with pytest.raises(ValueError, match="Invalid name"):
      Project.create(name=name, label="x")

  def test_env_is_read_back_unchanged(self, project):
    project.env = "A=1\nB=two words\n# note"
    project.save()
    assert Project.get_by_id("demo").env == "A=1\nB=two words\n# note"

  def test_an_invalid_env_is_refused_and_not_stored(self, project):
    project.env = "A=1\n9BAD=2"

    with pytest.raises(ValueError, match="project.env"):
      project.save()

    assert Project.get_by_id("demo").env is None

  def test_env_is_encrypted_in_the_database(self, db, project):
    project.env = "PASSWORD=a-very-distinctive-secret"
    project.save()

    raw = db.execute_sql("SELECT env FROM project WHERE name = 'demo'").fetchone()[0]

    assert "a-very-distinctive-secret" not in raw


class TestDeployConfig:
  def test_the_hash_is_pinned_because_a_change_marks_every_deployed_application_as_changed(self):
    assert DEPLOY_CONFIG.content_hash == "f8d26f1528f2d00750808c69e9e96a47f7e84ea2418d42ee71e55caab67ed380"

  def test_the_deploy_stamp_and_the_name_are_not_part_of_it(self):
    other = dataclasses.replace(DEPLOY_CONFIG, qualified_name="other-name", deploy_stamp="2030-01-01T00:00:00.000000")
    assert other.content_hash == DEPLOY_CONFIG.content_hash

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
      ("volumes", (("data", "/other"),)),
      ("volumes", ()),
  ])
  def test_every_deploy_input_is_part_of_it(self, field, value):
    changed = dataclasses.replace(DEPLOY_CONFIG, **{field: value})
    assert changed.content_hash != DEPLOY_CONFIG.content_hash

  def test_it_is_a_snapshot_that_cannot_be_changed(self):
    with pytest.raises(dataclasses.FrozenInstanceError):
      DEPLOY_CONFIG.env = "A=2"


class TestApplication:
  def test_the_qualified_name_joins_the_project_and_the_application(self, application):
    assert application.qualified_name == "demo-web"

  def test_the_name_is_unique_within_a_project(self, project, application):
    with pytest.raises(IntegrityError):
      Application.create(project=project, name="web", label="Again")

  def test_the_same_name_can_be_used_in_another_project(self, application):
    other = Project.create(name="other", label="Other")
    Application.create(project=other, name="web", label="Web")

  def test_a_project_with_applications_cannot_be_deleted(self, project, application):
    with pytest.raises(IntegrityError):
      project.delete_instance()

  def test_a_docker_application_drops_the_settings_that_belong_to_git(self, project):
    application = Application.create(
        project=project, name="docker", label="Docker", type="docker", image="nginx",
        repo="https://github.com/user/repo.git", path="other/Dockerfile", args="A=1", build_secrets="S=1")

    stored = Application.get_by_id(application.id)
    assert (stored.repo, stored.path, stored.args, stored.build_secrets) == (None, "Dockerfile", None, None)
    assert stored.image == "nginx"

  def test_a_git_application_builds_the_root_dockerfile_unless_told_otherwise(self, project):
    application = Application.create(project=project, name="git", label="Git", type="git", repo="https://github.com/user/repo.git")
    assert Application.get_by_id(application.id).path == "Dockerfile"

  def test_a_git_application_drops_the_image_and_keeps_its_build_settings(self, project):
    application = Application.create(
        project=project, name="git", label="Git", type="git", image="nginx",
        repo="https://github.com/user/repo.git", path="docker/Dockerfile", args="A=1", build_secrets="S=1")

    stored = Application.get_by_id(application.id)
    assert stored.image is None
    assert (stored.repo, stored.path, stored.args, stored.build_secrets) == (
        "https://github.com/user/repo.git", "docker/Dockerfile", "A=1", "S=1")

  @pytest.mark.parametrize("repo", [
      "https://github.com/user/repo.git",
      "http://git.example.test/user/repo.git",
      "https://github.com/user/repo.git#main",
      "https://github.com/user/repo.git#release-1.2",
      "https://github.com/user/repo.git:sub/dir",
      "https://github.com/user/repo.git#main:sub/dir",
  ])
  def test_a_repository_url_in_the_documented_format_is_accepted(self, project, repo):
    Application.create(project=project, name="git", label="Git", type="git", repo=repo)

  @pytest.mark.parametrize("repo", [
      "git@github.com:user/repo.git",
      "https://github.com/user/repo",
      "https://github.com/repo.git",
      "https://github.com/group/sub/repo.git",
      "ftp://github.com/user/repo.git",
      "https://github.com/user/repo.git#",
      "https://github.com/user/repo.git extra",
      "https://github.com/user/repo.git\n--evil",
      "https://github.com/user/repo.git\n",
      "https://github.com/user/repo.git#main\n",
  ])
  def test_any_other_repository_url_is_refused(self, project, repo):
    with pytest.raises(ValueError, match="Invalid Git repository URL"):
      Application.create(project=project, name="git", label="Git", type="git", repo=repo)

  @pytest.mark.parametrize("image", ["nginx", "nginx:1.27", "ghcr.io/org/app:v1.2", "registry.test:5000/app@sha256:abc123", "a_b-c.d"])
  def test_an_image_reference_is_accepted(self, project, image):
    Application.create(project=project, name="web2", label="Web", image=image)

  @pytest.mark.parametrize("image", ["-nginx", "nginx latest", "nginx;rm -rf /", "$(whoami)", "nginx\nlatest", "image`id`", "nginx'"])
  def test_an_image_reference_with_odd_characters_is_refused(self, project, image):
    with pytest.raises(ValueError, match="Invalid image reference"):
      Application.create(project=project, name="web2", label="Web", image=image)

  @pytest.mark.parametrize("field, label", [("env", "application.env"), ("args", "application.args"), ("build_secrets", "application.build_secrets")])
  def test_env_style_fields_are_checked_and_the_error_names_the_field(self, project, field, label):
    with pytest.raises(ValueError, match=label):
      Application.create(project=project, name="git", label="Git", type="git", **{field: "9BAD=1"})

  def test_env_is_read_back_unchanged(self, application):
    application.env = "A=1\nB=2"
    application.save()
    assert Application.get_by_id(application.id).env == "A=1\nB=2"


class TestApplicationDeployState:
  def test_the_stamp_is_none_until_the_first_deploy(self, application):
    assert application.deploy_stamp is None

  @pytest.mark.parametrize("deployed_at, stamp", [
      (datetime(2026, 10, 23, 10, 5, 7, 123456), "2026-10-23T10:05:07.123456"),
      (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05.000000"),
  ])
  def test_the_stamp_is_the_deploy_time_with_microseconds(self, application, deployed_at, stamp):
    application.deployed_at = deployed_at
    assert application.deploy_stamp == stamp

  def test_the_snapshot_holds_the_current_deploy_inputs(self, application):
    application.env = "A=1"
    application.command = "serve"
    application.project.env = "P=1"
    application.deployed_at = datetime(2026, 1, 2, 3, 4, 5)

    config = application.deploy_config

    assert config == DeployConfig(
        qualified_name="demo-web", deploy_stamp="2026-01-02T03:04:05.000000", type="docker", image="nginx", repo=None, path="Dockerfile",
        env="A=1", args="", build_secrets="", command="serve", project_env="P=1", volumes=())

  def test_unset_text_fields_are_empty_strings_in_the_snapshot(self, application):
    config = application.deploy_config
    assert (config.env, config.args, config.build_secrets, config.command, config.project_env) == ("", "", "", "", "")

  def test_volumes_are_sorted_so_creation_order_does_not_matter(self, application):
    Volume.create(application=application, name="zeta", path="/z")
    Volume.create(application=application, name="alpha", path="/a")

    assert application.deploy_config.volumes == (("alpha", "/a"), ("zeta", "/z"))


class TestConfigDirty:
  @pytest.fixture
  def deployed(self, application):
    application.deployed_hash = application.deploy_config.content_hash
    application.save()
    return application

  def test_an_application_never_deployed_is_not_dirty(self, application):
    application.env = "A=1"
    assert application.config_dirty is False

  def test_an_unchanged_deployed_application_is_not_dirty(self, deployed):
    assert deployed.config_dirty is False

  @pytest.mark.parametrize("field, value", [("env", "A=2"), ("image", "nginx:1.28"), ("command", "serve")])
  def test_changing_a_deploy_input_makes_it_dirty(self, deployed, field, value):
    setattr(deployed, field, value)
    assert deployed.config_dirty is True

  def test_changing_the_project_env_makes_it_dirty(self, deployed):
    deployed.project.env = "P=1"
    assert deployed.config_dirty is True

  def test_adding_a_volume_makes_it_dirty(self, deployed):
    Volume.create(application=deployed, name="data", path="/data")
    assert deployed.config_dirty is True

  def test_changing_only_the_label_or_the_deploy_time_does_not(self, deployed):
    deployed.label = "A new label"
    deployed.description = "Something else"
    deployed.deployed_at = datetime(2030, 1, 1)
    assert deployed.config_dirty is False

  def test_changing_it_back_clears_it(self, deployed):
    deployed.env = "A=2"
    deployed.env = None
    assert deployed.config_dirty is False


class TestDomain:
  def test_the_same_name_and_type_cannot_be_added_twice(self, application):
    Domain.create(application=application, name="shop", type="public", port=80)

    with pytest.raises(IntegrityError):
      Domain.create(application=application, name="shop", type="public", port=81)

  def test_the_same_name_can_exist_with_another_type(self, application):
    Domain.create(application=application, name="shop", type="public", port=80)
    Domain.create(application=application, name="shop", type="internal", port=80)

  def test_domains_go_with_their_application(self, application):
    Domain.create(application=application, name="shop", port=80)

    application.delete_instance()

    assert Domain.select().count() == 0


class TestContainer:
  def test_one_container_per_application_and_worker(self, application, worker):
    Container.create(application=application, worker=worker)

    with pytest.raises(IntegrityError):
      Container.create(application=application, worker=worker)

  def test_an_application_can_run_on_two_workers(self, application, worker):
    Container.create(application=application, worker=worker)
    Container.create(application=application, worker=Worker.create(hostname="w2", ip="100.64.0.3"))

  def test_the_tag_x_tag_is_reserved(self, application, worker):
    with pytest.raises(ValueError, match="reserved for internal use"):
      Container.create(application=application, worker=worker, domain_tag="x_tag")

  def test_other_tags_are_fine(self, application, worker):
    assert Container.create(application=application, worker=worker, domain_tag="blue").domain_tag == "blue"

  def test_an_application_with_a_container_cannot_be_deleted(self, application, worker):
    Container.create(application=application, worker=worker)

    with pytest.raises(IntegrityError):
      application.delete_instance()


class TestVolume:
  @pytest.mark.parametrize("path", ["/data", "/var/lib/my app", "/", "/a/b/c.d", "/data/"])
  def test_an_absolute_path_is_accepted(self, application, path):
    assert Volume.create(application=application, name="data", path=path).path == path

  @pytest.mark.parametrize("path", ["data", "./data", "", None, "/data:ro", "/data:/other", '/da"ta', "/da\nta", "/da\rta"])
  def test_a_relative_path_or_one_with_a_colon_quote_or_line_break_is_refused(self, application, path):
    with pytest.raises(ValueError, match="Invalid volume path"):
      Volume.create(application=application, name="data", path=path)

  def test_the_name_is_unique_within_an_application(self, application):
    Volume.create(application=application, name="data", path="/data")

    with pytest.raises(IntegrityError):
      Volume.create(application=application, name="data", path="/other")

  def test_volumes_go_with_their_application(self, application):
    Volume.create(application=application, name="data", path="/data")

    application.delete_instance()

    assert Volume.select().count() == 0


class TestEvent:
  @pytest.fixture
  def container(self, application, worker):
    return Container.create(application=application, worker=worker)

  def test_the_same_pair_of_task_ids_is_recorded_once_per_container(self, container):
    Event.create(container=container, type="deploy", application_task_id="a1", container_task_id="c1")

    with pytest.raises(IntegrityError):
      Event.create(container=container, type="deploy", application_task_id="a1", container_task_id="c1")

  def test_another_container_task_is_a_new_event(self, container):
    Event.create(container=container, type="deploy", application_task_id="a1", container_task_id="c1")
    Event.create(container=container, type="deploy", application_task_id="a1", container_task_id="c2")

  def test_events_go_with_their_container(self, container):
    Event.create(container=container, type="deploy", application_task_id="a1", container_task_id="c1")

    container.delete_instance()

    assert Event.select().count() == 0


class TestBackup:
  def test_backups_go_with_their_application(self, application):
    Backup.create(type="application", s3_path="backups/applications/x.tar.gz.enc", application=application)

    application.delete_instance()

    assert Backup.select().count() == 0


class TestTask:
  def test_the_json_fields_are_read_back_as_they_were_written(self, db):
    Task.create(task_id="t1", name="deploy", scopes=["application:1"], params={"force": True, "names": ["a"]}, executor="common",
                status="completed")

    task = Task.get_by_id("t1")

    assert task.scopes == ["application:1"]
    assert task.params == {"force": True, "names": ["a"]}

  def test_the_task_id_is_unique(self, db):
    Task.create(task_id="t1", name="deploy", executor="common", status="completed")

    with pytest.raises(IntegrityError):
      Task.create(task_id="t1", name="deploy", executor="common", status="completed")
