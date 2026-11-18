import pytest

from services.applications.updates import application_changed, container_changed, hosts_changed
from services.db.models import Application, Container, Worker
from services.updates import UpdateSource


@pytest.fixture
def hosted(application, worker):
  other = Worker.create(hostname="w2", subdomain="blue")
  return Container.create(application=application, worker=worker), Container.create(
      application=Application.create(project=application.project, name="api", label="Api"), worker=other)


def test_a_change_of_an_application_tells_its_page_only(signals, application):
  application_changed(application)
  assert signals == [(UpdateSource.APPLICATION, "demo/web")]


def test_a_change_of_a_container_tells_its_applications_page_and_its_workers(signals, hosted):
  container_changed(hosted[1])
  assert signals == [(UpdateSource.APPLICATION, "demo/api"), (UpdateSource.WORKER_CONTAINERS, "w2"),
                     (UpdateSource.DOMAINS, None)]


def test_a_change_of_what_the_workers_show_of_an_application_tells_each_worker_it_is_on_once(signals, hosted, application):
  Container.create(application=application, worker=Worker.create(hostname="w3", subdomain="green"))
  hosts_changed(application)
  assert sorted(signals, key=str) == sorted([
      (UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.WORKER_CONTAINERS, "w3"),
      (UpdateSource.DOMAINS, None)], key=str)


def test_an_application_with_no_containers_tells_only_itself(signals, application):
  hosts_changed(application)
  assert signals == [(UpdateSource.APPLICATION, "demo/web")]
