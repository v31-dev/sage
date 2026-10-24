import pytest

from services.db import Application, Container, Project, Worker


def application_count(project="demo"):
  return Project.get_by_id(project).application_count


def container_count(application):
  return Application.get_by_id(application.id).container_count


def status(application):
  return Application.get_by_id(application.id).status


@pytest.fixture
def workers(db):
  return [Worker.create(hostname=f"w{number}", ip=f"100.64.0.{number}") for number in (1, 2, 3)]


@pytest.fixture
def other_application(project):
  return Application.create(project=project, name="api", label="Api", image="nginx")


class TestApplicationCount:
  def test_each_new_application_is_counted(self, project):
    assert application_count() == 0

    Application.create(project=project, name="one", label="One")
    assert application_count() == 1

    Application.create(project=project, name="two", label="Two")
    assert application_count() == 2

  def test_a_deleted_application_is_no_longer_counted(self, application, other_application):
    assert application_count() == 2

    application.delete_instance()
    assert application_count() == 1

    other_application.delete_instance()
    assert application_count() == 0

  def test_each_project_counts_its_own(self, project, application):
    other = Project.create(name="other", label="Other")
    for name in ("one", "two", "three"):
      Application.create(project=other, name=name, label=name)

    assert (application_count("demo"), application_count("other")) == (1, 3)

    Application.get(Application.project == other).delete_instance()

    assert (application_count("demo"), application_count("other")) == (1, 2)

  def test_saving_an_existing_application_leaves_the_count_alone(self, application):
    application.label = "Renamed"
    application.save()
    assert application_count() == 1


class TestContainerCount:
  def test_each_new_container_is_counted(self, application, workers):
    assert container_count(application) == 0

    Container.create(application=application, worker=workers[0])
    assert container_count(application) == 1

    Container.create(application=application, worker=workers[1])
    assert container_count(application) == 2

  def test_a_deleted_container_is_no_longer_counted(self, application, workers):
    first = Container.create(application=application, worker=workers[0])
    Container.create(application=application, worker=workers[1])

    first.delete_instance()

    assert container_count(application) == 1

  def test_each_application_counts_its_own(self, application, other_application, workers):
    Container.create(application=application, worker=workers[0])
    Container.create(application=application, worker=workers[1])
    Container.create(application=other_application, worker=workers[0])

    assert (container_count(application), container_count(other_application)) == (2, 1)

  def test_saving_an_existing_container_leaves_the_count_alone(self, application, workers):
    container = Container.create(application=application, worker=workers[0])
    container.status = "active"
    container.save()
    assert container_count(application) == 1


class TestLastContainerGone:
  @pytest.fixture
  def running(self, application, workers):
    """The application is active with containers on two workers."""
    application.status = "active"
    application.save()
    return [Container.create(application=application, worker=workers[0]), Container.create(application=application, worker=workers[1])]

  def test_the_application_goes_inactive_when_its_last_container_is_deleted(self, application, running):
    running[0].delete_instance()
    running[1].delete_instance()

    assert status(application) == "inactive"

  def test_it_stays_as_it_was_while_a_container_remains(self, application, running):
    running[0].delete_instance()

    assert status(application) == "active"

  def test_another_application_is_not_touched(self, application, other_application, running, workers):
    other_application.status = "active"
    other_application.save()
    Container.create(application=other_application, worker=workers[2])

    running[0].delete_instance()
    running[1].delete_instance()

    assert (status(application), status(other_application)) == ("inactive", "active")

  def test_an_application_with_no_container_yet_keeps_its_status(self, application, other_application, workers):
    other_application.status = "deploying"
    other_application.save()
    container = Container.create(application=application, worker=workers[0])

    container.delete_instance()

    assert status(other_application) == "deploying"

  @pytest.mark.parametrize("before", ["active", "error", "deploying"])
  def test_whatever_the_status_was_it_becomes_inactive(self, application, workers, before):
    container = Container.create(application=application, worker=workers[0])
    application.status = before
    application.save()

    container.delete_instance()

    assert status(application) == "inactive"
