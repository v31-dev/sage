import pytest

from services.db.models import Container, Domain, Worker, WorkerEvent
from services.updates import UpdateSource
from services.workers.commands import add_worker, remove_worker, set_worker_subdomain
from utils.errors import Conflict


def stored(hostname):
  return Worker.get_by_id(hostname)


class TestAdd:
  def test_it_keeps_the_subdomain_it_is_given_trimmed(self, db):
    add_worker(" w1 ", " eu1 ")
    assert stored("w1").subdomain == "eu1"

  def test_a_worker_already_added_is_refused(self, worker):
    with pytest.raises(Conflict, match="Worker w1 is already added."):
      add_worker("w1", "eu1")

  def test_a_subdomain_another_worker_has_is_refused(self, db):
    add_worker("w1", "eu1")
    with pytest.raises(Conflict, match="A worker with subdomain eu1 already exists."):
      add_worker("w2", "eu1")
    assert Worker.select().count() == 1

  @pytest.mark.parametrize("hostname, subdomain, message", [
      ("", "eu1", "hostname is required."),
      (None, "eu1", "hostname is required."),
      ("w1", None, "A worker without a subdomain is not allowed."),
      ("w1", "  ", "A worker without a subdomain is not allowed."),
      ("W1", "eu1", "Invalid name"),
      ("w1", "EU1", "lowercase letters, digits and dashes"),
      ("w1", "-eu1", "lowercase letters, digits and dashes"),
  ])
  def test_an_invalid_worker_is_refused_and_not_stored(self, db, hostname, subdomain, message):
    with pytest.raises(ValueError, match=message):
      add_worker(hostname, subdomain)
    assert not Worker.select().exists()


class TestSubdomain:
  @pytest.fixture
  def internal(self, application, worker):
    container = Container.create(application=application, worker=worker)
    return Domain.create(name="shop", type="internal", port=80, container=container, namespace_worker=worker.hostname)

  def test_a_new_subdomain_is_stored_and_tells_the_workers_pages_and_those_of_its_internal_names(self, internal, signals):
    assert set_worker_subdomain("w1", " blue ") is True
    assert stored("w1").subdomain == "blue"
    assert set(signals) == {(UpdateSource.WORKER_LIST, None), (UpdateSource.WORKER_ENTRY, "w1"), (UpdateSource.DOMAINS, None),
                            (UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1")}

  def test_a_worker_without_domains_tells_its_own_pages(self, db, signals):
    add_worker("w1", "eu1")
    set_worker_subdomain("w1", "eu2")
    assert {(UpdateSource.WORKER_LIST, None), (UpdateSource.WORKER_ENTRY, "w1")} <= set(signals)

  def test_the_same_subdomain_changes_nothing_and_says_nothing(self, db, signals):
    add_worker("w1", "eu1")
    assert set_worker_subdomain("w1", "eu1") is False
    assert signals == []

  def test_one_another_worker_has_is_refused(self, db, signals):
    add_worker("w1", "eu1")
    add_worker("w2", "eu2")
    with pytest.raises(Conflict, match="A worker with subdomain eu1 already exists."):
      set_worker_subdomain("w2", "eu1")
    assert stored("w2").subdomain == "eu2"
    assert signals == []

  @pytest.mark.parametrize("subdomain, error, message", [
      (None, ValueError, "A worker without a subdomain is not allowed."),
      (" ", ValueError, "A worker without a subdomain is not allowed."),
      ("EU", ValueError, "lowercase letters, digits and dashes"),
  ])
  def test_an_invalid_subdomain_is_refused(self, db, subdomain, error, message):
    add_worker("w1", "eu1")
    with pytest.raises(error, match=message):
      set_worker_subdomain("w1", subdomain)
    assert stored("w1").subdomain == "eu1"

  def test_a_missing_worker_is_not_found(self, db):
    with pytest.raises(LookupError, match="Worker not found."):
      set_worker_subdomain("nobody", "eu1")


class TestRemove:
  def test_a_worker_with_nothing_on_it_is_removed_and_its_events_keep_its_name(self, worker):
    WorkerEvent.create(worker=worker, subject_id="w1", action="add", transition="succeeded")

    remove_worker("w1")

    assert not Worker.select().exists()
    assert (WorkerEvent.get().worker_id, WorkerEvent.get().subject_id) == (None, "w1")

  def test_a_worker_with_containers_is_refused(self, application, worker):
    Container.create(application=application, worker=worker)
    with pytest.raises(Conflict, match="A worker with containers can't be removed."):
      remove_worker("w1")
    assert Worker.select().exists()

  def test_a_worker_with_internal_domains_is_refused_even_unlinked(self, worker):
    Domain.create(name="shop", type="internal", port=80, namespace_worker=worker.hostname)
    with pytest.raises(Conflict, match="A worker with internal domains can't be removed."):
      remove_worker("w1")

  def test_a_missing_worker_is_not_found(self, db):
    with pytest.raises(LookupError, match="Worker not found."):
      remove_worker("nobody")
