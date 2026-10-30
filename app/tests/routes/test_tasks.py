from fastapi.encoders import jsonable_encoder

from services.db import Task
from services.logs import Logs
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


def line(task_id, sub_task_id, message):
  Logs().write_task_line({"task_id": task_id, "sub_task_id": sub_task_id, "ts": "2026-01-01T12:00:00.000000Z",
                          "level": "INFO", "logger": "services.manager", "message": message})


class TestLogs:
  def test_a_task_has_its_lines_and_its_parts_in_the_order_they_were_written(self, client, db):
    line("t1", "", "queued")
    line("t1", "c1", "deploying web")
    line("t2", "", "another task")
    line("t1", "c2", "deploying db")

    response = client.get("/api/tasks/t1/logs")

    assert [row["message"] for row in response.json()["lines"]] == ["queued", "deploying web", "deploying db"]
    assert response.json()["more"] is False

  def test_a_part_has_its_own_lines(self, client, db):
    line("t1", "c1", "deploying web")
    line("t1", "c2", "deploying db")

    response = client.get("/api/tasks/t1/logs", params={"sub_task_id": "c2"})

    [row] = response.json()["lines"]
    assert {key: row[key] for key in ("task_id", "sub_task_id", "ts", "level", "logger", "message")} == {
        "task_id": "t1", "sub_task_id": "c2", "ts": "2026-01-01T12:00:00.000000Z", "level": "INFO",
        "logger": "services.manager", "message": "deploying db"}

  def test_a_task_that_logged_nothing_has_no_lines(self, client, db):
    assert client.get("/api/tasks/t1/logs").json() == {"lines": [], "more": False}
