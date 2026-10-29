from fastapi.encoders import jsonable_encoder

from services.db import Task
from utils.common import utcnow


def finished(task_id, status="completed", **fields):
  return Task.create(task_id=task_id, name="deploy", scopes=["platform"], params={}, executor="app", status=status,
                     finished_at=utcnow(), **fields)


class TestQueue:
  def test_the_tasks_are_the_running_and_queued_ones_and_nothing_else(self, client, db):
    finished("t-0")

    response = client.get("/api/tasks/")

    assert response.json() == {"running": [{"task_id": "t-1"}], "queued": []}


class TestHistory:
  def test_the_finished_tasks_come_newest_first(self, client, db):
    for index in range(3):
      finished(f"t-{index}")

    response = client.get("/api/tasks/history")

    assert [task["task_id"] for task in response.json()] == ["t-2", "t-1", "t-0"]

  def test_a_task_that_is_running_is_not_in_the_history(self, client, db):
    finished("t-0")
    finished("t-1", status="running")

    assert [task["task_id"] for task in client.get("/api/tasks/history").json()] == ["t-0"]

  def test_only_so_many_come_back(self, client, db):
    for index in range(5):
      finished(f"t-{index}")

    response = client.get("/api/tasks/history", params={"limit": 2})

    assert [task["task_id"] for task in response.json()] == ["t-4", "t-3"]

  def test_a_hundred_come_back_unless_asked_for_fewer(self, client, db):
    for index in range(101):
      finished(f"t-{index}")

    assert len(client.get("/api/tasks/history").json()) == 100

  def test_a_row_has_the_fields_of_a_finished_task(self, client, db):
    finished("t-0", status="failed")

    [row] = client.get("/api/tasks/history").json()

    assert {"task_id", "name", "scopes", "params", "executor", "status", "created_at", "updated_at",
            "finished_at"} <= set(row)
    assert jsonable_encoder(row) == row
