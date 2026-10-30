import pytest

from services.db import Container, Event
from services.manager.application import container_part
from utils.logging import sub_task_id, task_id


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


@pytest.fixture
def in_task():
  token = task_id.set("t1")
  yield
  task_id.reset(token)


class TestContainerPart:
  def test_records_its_event_under_the_running_task_and_its_lines_carry_the_part(self, container, in_task):
    with container_part(container, "deploy") as part:
      assert sub_task_id.get() == part
    assert sub_task_id.get() == ""

    event = Event.get()
    assert (event.container_id, event.type, event.application_task_id, event.container_task_id) == (
        container.id, "deploy", "t1", part)

  def test_each_part_of_a_task_has_an_id_of_its_own(self, container, in_task):
    parts = []
    for _ in range(2):
      with container_part(container, "backup") as part:
        parts.append(part)

    assert parts[0] != parts[1]
    assert [event.container_task_id for event in Event.select().order_by(Event.id)] == parts

  def test_the_lines_after_a_part_that_failed_are_the_tasks_again(self, container, in_task):
    with pytest.raises(RuntimeError):
      with container_part(container, "stop"):
        raise RuntimeError("boom")

    assert sub_task_id.get() == ""
    assert Event.get().type == "stop"
