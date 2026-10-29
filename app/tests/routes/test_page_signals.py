import pytest

from services.db import Application, Container, Project, Worker
from services.updates import Updates, UpdateSource

APP = "/api/projects/demo/applications"


class Changes:
  def __init__(self):
    self.signals = []

  def changed(self, source, key=None):
    self.signals.append((source, key))


@pytest.fixture
def changes(register):
  return register(Updates, Changes())


@pytest.fixture
def hosted(application, worker, manager):
  """The application on two workers."""
  other = Worker.create(hostname="w2", ip="100.64.0.3")
  Container.create(application=application, worker=worker)
  Container.create(application=application, worker=other)
  return application


def said(changes):
  """What the API wrote told the pages, in no order; a repeat shows as a repeat."""
  return sorted(changes.signals, key=str)


WEB = (UpdateSource.APPLICATION, "demo/web")


class TestApplications:
  def test_a_new_application_tells_its_own_page_only(self, client, project, changes):
    assert client.post(f"{APP}/", json={"label": "Api", "image": "nginx"}).status_code == 200
    assert said(changes) == [(UpdateSource.APPLICATION, "demo/api")]

  def test_changing_one_tells_its_page_and_the_pages_of_the_workers_it_is_on(self, client, hosted, changes):
    assert client.put(f"{APP}/web", json={"label": "Renamed"}).status_code == 200
    assert said(changes) == sorted([WEB, (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.WORKER_CONTAINERS, "w2")], key=str)

  def test_deleting_one_tells_its_page(self, client, application, changes):
    assert client.delete(f"{APP}/web").status_code == 200
    assert said(changes) == [WEB]

  def test_a_change_that_is_refused_tells_nothing(self, client, hosted, changes):
    assert client.delete(f"{APP}/web").status_code == 409
    assert client.put(f"{APP}/web", json={"env": "9BAD=1"}).status_code == 400
    assert changes.signals == []


class TestContainers:
  def test_a_new_container_tells_its_applications_page_and_its_workers(self, client, application, worker, manager, changes):
    assert client.post(f"{APP}/web/containers/", json={"worker": "w1"}).status_code == 200
    assert said(changes) == sorted([WEB, (UpdateSource.WORKER_CONTAINERS, "w1")], key=str)

  def test_changing_one_tells_the_same_pages(self, client, application, worker, manager, changes):
    container = Container.create(application=application, worker=worker)
    assert client.put(f"{APP}/web/containers/w1", json={"domain_tag": "blue"}).status_code == 200
    assert container.id and said(changes) == sorted([WEB, (UpdateSource.WORKER_CONTAINERS, "w1")], key=str)

  def test_a_container_that_is_refused_tells_nothing(self, client, application, worker, manager, changes):
    Container.create(application=application, worker=worker)
    assert client.post(f"{APP}/web/containers/", json={"worker": "w1"}).status_code == 409
    assert client.post(f"{APP}/web/containers/", json={"worker": "nobody"}).status_code == 404
    assert changes.signals == []


class TestDomains:
  def test_each_write_tells_the_applications_page_only(self, client, application, manager, changes):
    assert client.post(f"{APP}/web/domains/", json={"name": "web", "type": "hosted", "port": 80}).status_code == 200
    assert said(changes) == [WEB]
    changes.signals.clear()
    response = client.put(f"{APP}/web/domains/hosted:web", json={"port": 8080})
    assert response.status_code == 200, response.json()
    assert said(changes) == [WEB]
    changes.signals.clear()
    assert client.delete(f"{APP}/web/domains/hosted:web").status_code == 200
    assert said(changes) == [WEB]

  def test_a_domain_that_is_refused_tells_nothing(self, client, application, manager, changes):
    assert client.post(f"{APP}/web/domains/", json={"name": "Not Valid", "type": "hosted", "port": 80}).status_code == 400
    assert changes.signals == []


class TestVolumes:
  def test_each_write_tells_the_applications_page_only(self, client, application, changes):
    assert client.post(f"{APP}/web/volumes/", json={"name": "data", "path": "/data"}).status_code == 200
    assert said(changes) == [WEB]
    changes.signals.clear()
    assert client.put(f"{APP}/web/volumes/data", json={"path": "/srv"}).status_code == 200
    assert said(changes) == [WEB]
    changes.signals.clear()
    assert client.delete(f"{APP}/web/volumes/data").status_code == 200
    assert said(changes) == [WEB]

  def test_a_volume_that_is_refused_tells_nothing(self, client, application, changes):
    assert client.post(f"{APP}/web/volumes/", json={"name": "data", "path": "relative"}).status_code == 400
    assert changes.signals == []


class TestProjects:
  def test_changing_a_project_tells_the_pages_of_its_applications_and_their_workers(self, client, hosted, changes):
    Application.create(project=Project.get_by_id("demo"), name="api", label="Api", image="nginx")
    assert client.put("/api/projects/demo", json={"label": "Renamed"}).status_code == 200
    assert said(changes) == sorted([WEB, (UpdateSource.APPLICATION, "demo/api"),
                                    (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.WORKER_CONTAINERS, "w2")], key=str)

  def test_a_project_without_applications_tells_nothing(self, client, project, changes):
    assert client.put("/api/projects/demo", json={"label": "Renamed"}).status_code == 200
    assert changes.signals == []

  def test_a_new_project_and_a_deleted_one_tell_nothing(self, client, changes):
    assert client.post("/api/projects/", json={"label": "Fresh"}).status_code == 200
    assert client.delete("/api/projects/fresh").status_code == 200
    assert changes.signals == []
