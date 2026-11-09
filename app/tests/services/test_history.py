from datetime import timedelta

import pytest

from services.db.models import Container, ContainerEvent, WorkerEvent
from services.history import purge_events, record_container_event, record_worker_event, task_parts
from services.logs import Logs
from utils.logging import sub_task_id, task_id


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


@pytest.fixture
def in_part():
  """Runs the test as part `p1` of task `t1`."""
  tokens = task_id.set("t1"), sub_task_id.set("p1")
  yield
  task_id.reset(tokens[0])
  sub_task_id.reset(tokens[1])


class TestContainerEvents:
  def test_an_event_records_the_running_task_and_part_and_what_the_container_was(self, container, in_part):
    assert record_container_event(container, "deploy", "admitted", operation_id="op1") is True

    event = ContainerEvent.get()
    assert (event.container_id, event.subject_id, event.application_name, event.worker_hostname) == (
        container.id, container.id, "demo-web", "w1")
    assert (event.action, event.transition, event.operation_id, event.task_id, event.part_id) == (
        "deploy", "admitted", "op1", "t1", "p1")

  def test_outside_a_task_it_has_none(self, container):
    record_container_event(container, "stop", "succeeded")
    assert (ContainerEvent.get().task_id, ContainerEvent.get().part_id) == (None, None)

  def test_a_repeated_dedupe_key_is_recorded_once(self, container):
    assert record_container_event(container, "deploy", "succeeded", dedupe_key="op1:succeeded") is True
    assert record_container_event(container, "deploy", "succeeded", dedupe_key="op1:succeeded") is False
    assert ContainerEvent.select().count() == 1

  def test_a_reason_is_kept_as_structured_data(self, container):
    record_container_event(container, "deploy", "failed", reason={"phase": "pull", "error": "timeout"})
    assert ContainerEvent.get().reason == {"phase": "pull", "error": "timeout"}


class TestWorkerEvents:
  def test_an_event_of_a_worker_is_linked_to_its_row(self, worker):
    record_worker_event("w1", "reboot", "admitted", operation_id="op1")
    assert (WorkerEvent.get().worker_id, WorkerEvent.get().subject_id) == ("w1", "w1")

  def test_an_event_of_a_hostname_that_is_no_worker_keeps_only_its_name(self, db):
    assert record_worker_event("w9", "add", "rejected", reason={"owner": "another Sage"}) is True
    assert (WorkerEvent.get().worker_id, WorkerEvent.get().subject_id, WorkerEvent.get().reason) == (
        None, "w9", {"owner": "another Sage"})

  def test_a_repeated_dedupe_key_is_recorded_once(self, worker):
    record_worker_event("w1", "connectivity", "observed", dedupe_key="w1:offline:1")
    assert record_worker_event("w1", "connectivity", "observed", dedupe_key="w1:offline:1") is False
    assert WorkerEvent.select().count() == 1


class TestTaskParts:
  @pytest.fixture
  def logged(self, register):
    class Parts:
      def query_task_parts(self, task):
        return {"t1": ["p2", "p1", "p3"]}.get(task, [])
    return register(Logs, Parts())

  def test_each_logged_part_has_the_action_and_worker_of_its_first_event_in_the_order_it_logged(
          self, container, logged, in_part):
    record_container_event(container, "deploy", "admitted")
    record_container_event(container, "reset", "succeeded")
    token = sub_task_id.set("p2")
    record_container_event(container, "delete", "admitted")
    sub_task_id.reset(token)

    assert task_parts("t1") == [{"id": "p2", "type": "delete", "worker": "w1"}, {"id": "p1", "type": "deploy", "worker": "w1"},
                                {"id": "p3", "type": None, "worker": None}]

  def test_a_part_keeps_its_worker_after_its_container_is_deleted(self, container, logged, in_part):
    record_container_event(container, "delete", "admitted")
    container.delete_instance()

    assert task_parts("t1")[1] == {"id": "p1", "type": "delete", "worker": "w1"}


def test_purging_deletes_the_events_of_both_kinds_recorded_before_the_cutoff(container, worker, clock):
  record_container_event(container, "deploy", "succeeded")
  record_worker_event("w1", "reboot", "succeeded")
  clock.advance(timedelta(days=2))
  record_container_event(container, "stop", "succeeded")
  record_worker_event("w1", "repair", "succeeded")

  assert purge_events(clock.current - timedelta(days=1)) == 2

  assert [event.action for event in ContainerEvent.select()] == ["stop"]
  assert [event.action for event in WorkerEvent.select()] == ["repair"]
