from datetime import datetime, timedelta

import pytest
from fastapi.encoders import jsonable_encoder

import config
from services.db import Container, Event, Task
from services.logs import Logs

T0 = datetime(2026, 1, 1, 12, 0)


def minutes(count):
  return T0 + timedelta(minutes=count)


def finished(task_id, status="completed", queued=0, ended=1, **fields):
  """A finished task, queued and finished so many minutes after T0."""
  values = {"name": "deploy", "scopes": ["platform"], "params": {}, "executor": "app", **fields}
  return Task.create(task_id=task_id, status=status, created_at=minutes(queued), started_at=minutes(queued),
                     finished_at=minutes(ended), **values)


def history(client, **params):
  return client.get("/api/tasks/history", params=params).json()


def listed(client, **params):
  return [task["task_id"] for task in history(client, **params)["tasks"]]


class TestQueue:
  def test_the_tasks_are_the_running_and_queued_ones_and_nothing_else(self, client, db):
    finished("t-0")

    response = client.get("/api/tasks/")

    assert response.json() == {"running": [{"task_id": "t-1"}], "queued": []}


class TestHistory:
  def test_the_finished_tasks_come_newest_finished_first(self, client, db):
    finished("t-a", queued=0, ended=10)
    finished("t-b", queued=5, ended=6)
    finished("t-c", queued=1, ended=8)

    assert listed(client) == ["t-a", "t-c", "t-b"]

  def test_a_task_that_is_running_is_not_in_the_history(self, client, db):
    finished("t-0")
    finished("t-1", status="running")

    assert listed(client) == ["t-0"]

  def test_the_newest_asked_for_come_back_with_how_many_match(self, client, db):
    for index in range(5):
      finished(f"t-{index}", ended=index)

    page = history(client, limit=2)

    assert [task["task_id"] for task in page["tasks"]] == ["t-4", "t-3"]
    assert page["total"] == 5

  def test_the_most_a_list_holds_come_back_unless_asked_otherwise(self, client, db):
    Task.insert_many([{"task_id": f"t-{index}", "name": "deploy", "scopes": ["platform"], "params": {}, "executor": "app",
                       "status": "completed", "created_at": T0, "finished_at": T0} for index in range(config.LIST_MAX_ROWS + 1)]).execute()

    page = history(client)

    assert len(page["tasks"]) == config.LIST_MAX_ROWS and page["total"] == config.LIST_MAX_ROWS + 1

  @pytest.mark.parametrize("limit", [0, config.LIST_MAX_ROWS + 1])
  def test_a_limit_out_of_bounds_is_refused(self, client, db, limit):
    assert client.get("/api/tasks/history", params={"limit": limit}).status_code == 422

  def test_a_row_has_the_fields_of_a_finished_task(self, client, db):
    finished("t-0", status="failed")

    [row] = history(client)["tasks"]

    assert {"task_id", "name", "scopes", "params", "executor", "status", "created_at", "started_at", "updated_at",
            "finished_at"} <= set(row)
    assert jsonable_encoder(row) == row

  @pytest.mark.parametrize("params, expected", [
      ({"name": "deploy"}, ["t-deploy"]),
      ({"executor": "platform"}, ["t-platform"]),
      ({"status": "failed"}, ["t-failed"]),
      ({"name": "deploy", "status": "failed"}, []),
  ])
  def test_a_name_an_executor_and_a_status_match_exactly_and_together(self, client, db, params, expected):
    finished("t-deploy", name="deploy")
    finished("t-platform", name="sync_workers", executor="platform")
    finished("t-failed", name="deploy_container", status="failed")

    assert listed(client, **params) == expected

  @pytest.mark.parametrize("scope, expected", [
      ("app", ["t-app", "t-web", "t-web2", "t-both", "t-under"]),
      ("app:demo-web", ["t-web"]),
      ("platform", ["t-both"]),
      ("app:a_b", []),
      ("app:a%b", []),
      ("app:demo*", ["t-web", "t-web2"]),
      ("*web", ["t-web"]),
      ("APP:Demo-Web", ["t-web"]),
      ("*", ["t-app", "t-web", "t-web2", "t-both", "t-metrics", "t-apps", "t-under"]),
  ])
  def test_a_scope_matches_itself_and_its_children_in_any_case_with_star_for_any_characters(self, client, db, scope,
                                                                                            expected):
    for index, (task_id, scopes) in enumerate([("t-app", ["app"]), ("t-web", ["app:demo-web"]),
                                               ("t-web2", ["app:demo-web2"]), ("t-both", ["app:x", "platform"]),
                                               ("t-metrics", ["metrics"]), ("t-apps", ["apps"]),
                                               ("t-under", ["app:aXb:c"])]):
      finished(task_id, scopes=scopes, ended=-index)

    assert listed(client, scope=scope) == expected

  def test_a_window_matches_a_task_active_at_any_point_in_it(self, client, db):
    for task_id, queued, ended in [("before", 0, 5), ("ends-as-it-opens", 0, 10), ("over-the-start", 5, 15),
                                   ("inside", 12, 18), ("over-the-end", 15, 25), ("queued-as-it-closes", 20, 30),
                                   ("after", 25, 30), ("across", 0, 31)]:
      finished(task_id, queued=queued, ended=ended)

    matched = listed(client, from_ts=minutes(10).isoformat(), to_ts=minutes(20).isoformat())

    assert sorted(matched) == sorted(["ends-as-it-opens", "over-the-start", "inside", "over-the-end",
                                      "queued-as-it-closes", "across"])

  def test_a_window_open_at_one_end_is_bounded_at_the_other_only(self, client, db):
    finished("early", queued=0, ended=5)
    finished("late", queued=20, ended=30)

    assert listed(client, from_ts=minutes(10).isoformat()) == ["late"]
    assert listed(client, to_ts=minutes(10).isoformat()) == ["early"]

  def test_a_time_with_a_zone_is_taken_in_utc(self, client, db):
    finished("early", queued=0, ended=5)
    finished("late", queued=20, ended=30)

    assert listed(client, from_ts="2026-01-01T17:40:00+05:30") == ["late"]
    assert listed(client, to_ts="2026-01-01T12:10:00Z") == ["early"]


class TestHistoryFilters:
  def test_the_choices_are_the_names_and_executors_stored_and_the_statuses(self, client, db):
    finished("t-1", name="sync_workers", executor="platform")
    finished("t-2", name="deploy", executor="app")
    finished("t-3", name="deploy", executor="app", status="failed")

    assert client.get("/api/tasks/history/filters").json() == {
        "names": ["deploy", "sync_workers"], "executors": ["app", "platform"],
        "statuses": ["completed", "failed", "cancelled"]}


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

  def test_a_part_has_its_own_lines(self, client, db):
    line("t1", "c1", "deploying web")
    line("t1", "c2", "deploying db")

    response = client.get("/api/tasks/t1/logs", params={"sub_task_id": "c2"})

    [row] = response.json()["lines"]
    assert {key: row[key] for key in ("task_id", "sub_task_id", "ts", "level", "logger", "message")} == {
        "task_id": "t1", "sub_task_id": "c2", "ts": "2026-01-01T12:00:00.000000Z", "level": "INFO",
        "logger": "services.manager", "message": "deploying db"}

  def test_a_view_gets_the_lines_after_the_last_one_it_holds(self, client, db):
    line("t1", "", "first")
    line("t1", "", "second")
    held = client.get("/api/tasks/t1/logs").json()["lines"][0]["id"]

    response = client.get("/api/tasks/t1/logs", params={"after_id": held})

    assert [row["message"] for row in response.json()["lines"]] == ["second"]

  def test_a_task_that_logged_nothing_has_no_lines(self, client, db):
    assert client.get("/api/tasks/t1/logs").json() == {"lines": []}


class TestParts:
  def test_the_parts_that_logged_come_in_the_order_of_their_first_line_with_their_events_kind_and_worker(
          self, client, application, worker):
    container = Container.create(application=application, worker=worker)
    Event.create(container=container, type="deploy", application_task_id="t1", container_task_id="c2")
    Event.create(container=container, type="backup", application_task_id="t1", container_task_id="c9")
    line("t1", "", "queued")
    line("t1", "c2", "deploying web")
    line("t1", "c1", "deleting db")
    line("t1", "c2", "deployed web")
    line("t2", "c3", "another task")

    assert client.get("/api/tasks/t1/parts").json() == [
        {"id": "c2", "type": "deploy", "worker": "w1"}, {"id": "c1", "type": None, "worker": None}]

  def test_a_task_without_parts_has_none(self, client, db):
    line("t1", "", "queued")

    assert client.get("/api/tasks/t1/parts").json() == []
