import pytest

from services.applications import commands
from services.applications.commands import (attach_container, create_application, create_project, create_volume, delete_application, delete_project,
                                            delete_volume, detach_container, patch_application, patch_project, patch_volume)
from services.db.models import Application, Container, ContainerEvent, Domain, Project, Volume, Worker
from services.updates import UpdateSource
from utils.errors import Conflict

GIT = {"type": "git", "repo": "https://github.com/user/repo.git", "path": "docker/Dockerfile", "args": "A=1",
       "build_secrets": "S=1"}
GIT_FIELDS = ("image", "repo", "path", "args", "build_secrets")


def stored(row):
  return type(row).get_by_id(row.get_id())


def fields(row, names):
  return tuple(getattr(row, name) for name in names)


class TestProject:
  def test_it_is_named_from_its_trimmed_label(self, db):
    project = create_project("  My Shop 2 ", " Sells things ")
    assert (stored(project).name, stored(project).label, stored(project).description) == ("myshop2", "My Shop 2", "Sells things")

  def test_a_label_that_gives_no_name_is_refused(self, db):
    with pytest.raises(ValueError, match="does not produce a valid name"):
      create_project("123")

  def test_a_second_project_with_the_same_name_is_refused(self, project):
    with pytest.raises(Conflict, match="A project named 'demo' already exists."):
      create_project("De-mo")

  def test_patches_of_different_fields_keep_each_other(self, project):
    patch_project("demo", {"label": "From the first"})
    patch_project("demo", {"description": "From the second"})
    assert (stored(project).label, stored(project).description) == ("From the first", "From the second")

  def test_an_invalid_env_is_refused_and_nothing_of_the_patch_is_written(self, project, signals):
    with pytest.raises(ValueError, match="project.env"):
      patch_project("demo", {"label": "Renamed", "env": "9BAD=1"})
    assert (stored(project).label, stored(project).env) == ("Demo", None)
    assert signals == []

  def test_a_patch_tells_the_page_of_each_of_its_applications(self, application, signals):
    patch_project("demo", {"label": "Renamed"})
    assert signals == [(UpdateSource.APPLICATION, "demo/web")]

  def test_a_patch_that_changes_nothing_says_nothing(self, application, signals):
    patch_project("demo", {"label": " Demo "})
    assert signals == []

  def test_an_unknown_field_is_refused(self, project):
    with pytest.raises(ValueError, match="Unknown fields: name."):
      patch_project("demo", {"name": "other"})

  def test_a_required_field_cannot_be_cleared(self, project):
    with pytest.raises(ValueError, match="label is required."):
      patch_project("demo", {"label": None})

  def test_a_missing_project_is_not_found(self, db):
    with pytest.raises(LookupError, match="Project not found."):
      patch_project("nope", {"label": "x"})

  def test_a_project_with_applications_cannot_be_deleted_and_one_without_can(self, application):
    with pytest.raises(Conflict, match="A project with applications can't be deleted."):
      delete_project("demo")

    delete_application(application.id)
    delete_project("demo")

    assert not Project.select().exists()


class TestApplication:
  def test_it_is_named_from_its_label_and_counted_in_its_project(self, project, signals):
    application = create_application("demo", " Web App ")
    create_application("demo", "Api")

    assert (stored(application).name, stored(application).label) == ("webapp", "Web App")
    assert stored(project).application_count == 2
    assert signals == [(UpdateSource.APPLICATION, "demo/webapp"), (UpdateSource.APPLICATION, "demo/api")]

  def test_a_second_application_with_the_same_name_in_the_project_is_refused(self, application):
    with pytest.raises(Conflict, match="An application named 'web' already exists in this project."):
      create_application("demo", "Web")
    assert stored(application.project).application_count == 0

  def test_an_application_in_a_missing_project_is_not_found(self, db):
    with pytest.raises(LookupError, match="Project not found."):
      create_application("nope", "Web")

  def test_deleting_it_uncounts_it_and_takes_its_volumes(self, project, signals):
    application = create_application("demo", "Web")
    create_volume(application.id, "data", "/data")

    delete_application(application.id)

    assert stored(project).application_count == 0
    assert not Volume.select().exists()
    assert signals[-1] == (UpdateSource.APPLICATION, "demo/web")

  def test_one_with_containers_cannot_be_deleted(self, application, worker):
    attach_container(application.id, worker.hostname)
    with pytest.raises(Conflict, match="An application with containers can't be deleted."):
      delete_application(application.id)
    assert Application.select().exists()

  def test_switching_to_git_keeps_the_git_inputs_and_drops_the_image(self, application):
    patch_application(application.id, GIT)
    assert fields(stored(application), GIT_FIELDS) == (None, GIT["repo"], "docker/Dockerfile", "A=1", "S=1")

  def test_a_later_patch_of_another_field_keeps_the_git_inputs_a_stale_view_did_not_have(self, application):
    stale = Application.get_by_id(application.id)
    patch_application(application.id, GIT)

    patch_application(stale.id, {"label": "Renamed"})

    assert fields(stored(application), GIT_FIELDS) == (None, GIT["repo"], "docker/Dockerfile", "A=1", "S=1")
    assert stored(application).label == "Renamed"

  def test_a_patch_of_another_field_leaves_the_type_fields_as_they_are_stored(self, application):
    Application.update(repo=GIT["repo"]).where(Application.id == application.id).execute()

    patch_application(application.id, {"label": "Renamed"})

    assert stored(application).repo == GIT["repo"]

  def test_git_inputs_sent_to_a_docker_application_are_dropped(self, application):
    patch_application(application.id, {"repo": GIT["repo"], "args": "A=1", "path": "other/Dockerfile"})
    assert fields(stored(application), GIT_FIELDS) == ("nginx", None, "Dockerfile", None, None)

  def test_switching_back_to_docker_drops_the_git_inputs(self, application):
    patch_application(application.id, GIT)
    patch_application(application.id, {"type": "docker", "image": " nginx:1.27 "})
    assert fields(stored(application), GIT_FIELDS) == ("nginx:1.27", None, "Dockerfile", None, None)

  @pytest.mark.parametrize("patch, message", [
      ({"image": "nginx latest"}, "Invalid image reference"),
      ({"type": "git", "repo": "git@github.com:user/repo.git"}, "Invalid Git repository URL"),
      ({"type": "bogus"}, "Invalid choice 'bogus'"),
      ({"env": "9BAD=1"}, "application.env"),
      ({"type": "git", "args": "9BAD=1"}, "application.args"),
      ({"type": "git", "build_secrets": "9BAD=1"}, "application.build_secrets"),
  ])
  def test_an_invalid_value_is_refused_and_nothing_of_the_patch_is_written(self, application, signals, patch, message):
    with pytest.raises(ValueError, match=message):
      patch_application(application.id, {"label": "Renamed", **patch})
    assert (stored(application).label, stored(application).type, stored(application).image) == ("Web", "docker", "nginx")
    assert signals == []

  def test_a_patch_tells_the_application_and_the_workers_it_runs_on(self, application, worker, signals):
    attach_container(application.id, worker.hostname)
    signals.clear()

    patch_application(application.id, {"label": "Renamed"})

    assert signals == [(UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1"),
                       (UpdateSource.DOMAINS, None)]


class TestVolume:
  def test_it_is_created_trimmed_with_a_blank_cron_as_none(self, application, signals):
    volume = create_volume(application.id, " data ", " /data ", "  ")
    assert (stored(volume).name, stored(volume).path, stored(volume).backup_cron) == ("data", "/data", None)
    assert signals == [(UpdateSource.APPLICATION, "demo/web")]

  @pytest.mark.parametrize("name, path, cron, error, message", [
      ("data", "data", None, ValueError, "Invalid volume path"),
      ("data", "/data", "0 3 * *", ValueError, "5-part"),
      (None, "/data", None, ValueError, "name is required."),
      ("Data", "/data", None, ValueError, "Invalid name"),
  ])
  def test_an_invalid_volume_is_refused(self, application, name, path, cron, error, message):
    with pytest.raises(error, match=message):
      create_volume(application.id, name, path, cron)
    assert not Volume.select().exists()

  def test_a_second_volume_with_the_same_name_is_refused_on_create_and_on_rename(self, application):
    create_volume(application.id, "data", "/data")
    other = create_volume(application.id, "logs", "/logs")

    with pytest.raises(Conflict, match="A volume named 'data' already exists in this application."):
      create_volume(application.id, "data", "/other")
    with pytest.raises(Conflict, match="A volume named 'data' already exists in this application."):
      patch_volume(other.id, {"name": "data"})

  def test_a_rename_keeps_its_identity(self, application):
    volume = create_volume(application.id, "data", "/data", "0 3 * * *")

    patch_volume(volume.id, {"name": "files", "backup_cron": ""})

    renamed = stored(volume)
    assert (renamed.id, renamed.name, renamed.path, renamed.backup_cron) == (volume.id, "files", "/data", None)

  def test_a_patch_that_changes_nothing_says_nothing(self, application, signals):
    volume = create_volume(application.id, "data", "/data")
    signals.clear()

    patch_volume(volume.id, {"name": "data", "path": " /data ", "backup_cron": ""})

    assert signals == []

  def test_deleting_it_tells_its_application(self, application, signals):
    volume = create_volume(application.id, "data", "/data")
    delete_volume(volume.id)
    assert not Volume.select().exists()
    assert signals[-1] == (UpdateSource.APPLICATION, "demo/web")


class TestContainer:
  def test_attaching_counts_it_and_tells_its_pages(self, application, worker, signals):
    container = attach_container(application.id, worker.hostname)

    assert stored(application).container_count == 1
    assert stored(container).worker_id == "w1"
    assert signals == [(UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1"),
                       (UpdateSource.DOMAINS, None)]

  def test_a_second_container_on_the_same_worker_is_refused(self, application, worker):
    attach_container(application.id, worker.hostname)
    with pytest.raises(Conflict, match="Container already exists on worker 'w1'."):
      attach_container(application.id, worker.hostname)
    assert stored(application).container_count == 1

  def test_a_missing_worker_is_not_found(self, application):
    with pytest.raises(LookupError, match="Worker with hostname 'nobody' not found."):
      attach_container(application.id, "nobody")

  def test_detaching_uncounts_it_unlinks_its_domains_and_keeps_its_events(self, application, worker, signals):
    container = attach_container(application.id, worker.hostname)
    linked = Domain.create(name="shop", type="public", port=80, container=container)
    Domain.create(name="other", type="public", port=80)
    ContainerEvent.create(container=container, subject_id=container.id, action="deploy", transition="succeeded",
                          application_name="demo-web", worker_hostname="w1")
    signals.clear()

    assert detach_container(container.id) == [linked.id]

    assert stored(application).container_count == 0
    assert stored(linked).container_id is None
    assert ContainerEvent.get().container_id is None
    assert signals == [(UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1"),
                       (UpdateSource.DOMAINS, None)]

  def test_detaching_a_missing_container_is_not_found(self, db):
    with pytest.raises(LookupError, match="Container not found."):
      detach_container("0123456789abcdef")


class TestTransaction:
  def test_a_command_inside_another_transaction_is_refused(self, project):
    with Project._meta.database.atomic():
      with pytest.raises(RuntimeError, match="runs in its own transaction"):
        patch_project("demo", {"label": "Renamed"})
    assert stored(project).label == "Demo"

  def test_a_membership_write_that_fails_leaves_neither_the_row_nor_the_count_and_says_nothing(
          self, application, worker, signals, monkeypatch):
    def fail(application_id):
      raise RuntimeError("planted")
    monkeypatch.setattr(commands, "_count_containers", fail)

    with pytest.raises(RuntimeError, match="planted"):
      attach_container(application.id, worker.hostname)

    assert not Container.select().exists()
    assert signals == []

  def test_a_worker_row_is_left_alone(self, application, worker):
    attach_container(application.id, worker.hostname)
    assert Worker.select().count() == 1
