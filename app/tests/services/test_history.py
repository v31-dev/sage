from datetime import datetime, timedelta

import pytest

from services.db.models import Container, ContainerEvent, Notification, Task, WorkerEvent
from services.history import purge_history, record_container_event, record_task, record_worker_event, task_history, task_history_filters
from services.logs import Logs
from utils.logging import task_id
from utils.queue import QueuedTask


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


@pytest.fixture
def in_task():
  """Runs the test as task `t1`."""
  token = task_id.set("t1")
  yield
  task_id.reset(token)


class TestContainerEvents:
  def test_an_event_records_the_running_task_and_what_the_container_was(self, container, in_task):
    assert record_container_event(container, "deploy", "admitted", operation_id="op1") is True

    event = ContainerEvent.get()
    assert (event.container_id, event.subject_id, event.application_name, event.worker_hostname) == (
        container.id, container.id, "demo-web", "w1")
    assert (event.action, event.transition, event.operation_id, event.task_id) == ("deploy", "admitted", "op1", "t1")

  def test_outside_a_task_it_has_none(self, container):
    record_container_event(container, "stop", "succeeded")
    assert ContainerEvent.get().task_id is None

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


def test_purging_deletes_the_history_older_than_its_days_and_keeps_the_rest(container, worker, clock):
  record_container_event(container, "deploy", "succeeded")
  record_worker_event("w1", "reboot", "succeeded")
  Notification.create(content="Deployed.")
  record_task(finished("t-old"), "completed")
  clock.advance(timedelta(days=14))
  record_container_event(container, "stop", "succeeded")
  record_worker_event("w1", "repair", "succeeded")
  Notification.create(content="Stopped.")
  record_task(finished("t-new"), "completed")
  clock.advance(timedelta(seconds=1))

  assert purge_history(14) == 4

  assert [event.action for event in ContainerEvent.select()] == ["stop"]
  assert [event.action for event in WorkerEvent.select()] == ["repair"]
  assert [notification.content for notification in Notification.select()] == ["Stopped."]
  assert [task.task_id for task in Task.select()] == ["t-new"]


def finished(task_id_="t1", key=("lifecycle", "c1"), labels=None, scopes=("worker:w1:container:c1",), name="deploy",
             **fields):
  return QueuedTask(name=name, key=key, scopes=frozenset(scopes), task=lambda: None, executor="app", task_id=task_id_,
                    params={"container_id": "c1"}, inputs={"snapshot": {"secret": "s"}},
                    labels={"Application": "shop / web", "Worker": "w1"} if labels is None else labels, **fields)


class TestTaskRecords:
  def test_a_finished_task_keeps_what_identifies_it_and_what_people_call_it_but_not_its_inputs(self, db, clock):
    task = finished(queued_at=datetime(2026, 1, 1, 11, 58), started_at=datetime(2026, 1, 1, 11, 59))
    row = record_task(task, "completed")

    stored = Task.get()
    assert (stored.task_id, stored.name, stored.scopes, stored.params, stored.labels, stored.executor, stored.status) == (
        "t1", "deploy", ["worker:w1:container:c1"], {"container_id": "c1"},
        {"Application": "shop / web", "Worker": "w1"}, "app", "completed")
    assert (stored.created_at, stored.started_at, stored.finished_at) == (
        datetime(2026, 1, 1, 11, 58), datetime(2026, 1, 1, 11, 59), datetime(2026, 1, 1, 12, 0))
    assert row["task_id"] == "t1" and "secret" not in str(row)

  def test_a_task_cancelled_before_it_started_has_no_start(self, db):
    record_task(finished(), "cancelled")
    assert Task.get().started_at is None


class TestTaskHistory:
  @pytest.fixture
  def tasks(self, db, clock):
    for index, (labels, scopes) in enumerate([
        ({"Application": "shop / web", "Worker": "w1"}, ["worker:w1:container:c1"]),
        ({"Application": "shop / api", "Worker": "w2"}, ["worker:w2:container:c2"]),
        ({"Domain": "100%_off.example.com"}, ["domain:id:d1"]),
        ({}, ["certificate"]),
    ]):
      record_task(finished(f"t{index}", labels=labels, scopes=scopes), "completed")
      clock.advance(timedelta(minutes=1))
    Task.insert(task_id="old", name="deploy", scopes=["worker:w1"], params={}, executor="app", status="failed",
                finished_at=clock.current).execute()

  def ids(self, **filters):
    return sorted(task["task_id"] for task in task_history(**filters)["tasks"])

  @pytest.mark.parametrize("label, expected", [
      ("web", ["t0"]), ("SHOP", ["t0", "t1"]), ("w2", ["t1"]), ("100%_off", ["t2"]), ("100%x", []), ("_", ["t2"]),
      ("Application", [])])
  def test_a_label_matches_any_label_holding_the_text_in_any_case(self, tasks, label, expected):
    assert self.ids(label=label) == expected

  def test_a_scope_still_matches_itself_and_its_children(self, tasks):
    assert self.ids(scope="worker:w1") == ["old", "t0"]
    assert self.ids(scope="worker:w*") == ["old", "t0", "t1"]

  def test_filters_combine(self, tasks):
    assert self.ids(label="shop", scope="worker:w2") == ["t1"]

  def test_the_newest_finished_come_first_with_how_many_match(self, tasks):
    result = task_history(label="shop", limit=1)
    assert ([task["task_id"] for task in result["tasks"]], result["total"]) == (["t1"], 2)

  def test_the_filters_offer_the_stored_names_and_executors(self, tasks):
    assert task_history_filters() == {"names": ["deploy"], "executors": ["app"],
                                      "statuses": ["completed", "failed", "cancelled"]}
