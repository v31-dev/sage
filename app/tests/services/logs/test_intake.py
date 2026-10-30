import json
from datetime import datetime, timezone

import pytest
from peewee import OperationalError

from services.updates import UpdateFeed

from .builders import access_line, event, push, stamp


class TestIngest:
  def test_an_applications_lines_go_to_its_shard_with_the_worker(self, logs):
    skipped = logs.ingest("w1", push(event("demo-web", "hello", 0), event("demo-web", "world", 1, "stderr"),
                                     event("demo-api", "other", 2)))

    assert skipped == 0
    assert [(row["hostname"], row["ts"], row["stream"], row["message"]) for row in logs.query_application_lines("demo-web")["lines"]] == [
        ("w1", stamp(0), "stdout", "hello"), ("w1", stamp(1), "stderr", "world")]
    assert [row["message"] for row in logs.query_application_lines("demo-api")["lines"]] == ["other"]

  def test_traefiks_access_lines_are_requests_in_columns(self, logs):
    logs.ingest("w1", push(event("traefik", access_line(3))))

    [row] = logs._store("requests").lines.select().dicts()
    assert {key: value for key, value in row.items() if key != "id"} == {
        "ts": stamp(3), "worker": "w1", "host": "web.example.com", "method": "GET", "path": "/items/3?page=2",
        "status": 200, "duration_ms": 12.5, "router": "demo-web@file", "service": "demo-web@file",
        "client": "100.64.0.9", "size": 1543}

  def test_traefiks_own_lines_are_dropped(self, logs):
    logs.ingest("w1", push(event("traefik", "2026-01-01T12:00:00Z INF Configuration loaded from file"),
                           event("traefik", json.dumps({"level": "error", "msg": "a JSON line of its own"})),
                           event("traefik", access_line())))

    assert logs._store("requests").count() == 1
    assert not (logs.db_path / "logs" / "traefik.db").exists()

  def test_a_line_that_is_not_an_event_or_of_a_name_that_cannot_be_a_file_is_skipped(self, logs):
    skipped = logs.ingest("w1", push("not json", json.dumps({"container_name": "demo-web"}), "[1, 2]",
                                     event("Not_A_Name", "x"), event("tasks", "x"), event("requests", "x"),
                                     event("demo-web", "kept")))

    assert skipped == 6
    assert [row["message"] for row in logs.query_application_lines("demo-web")["lines"]] == ["kept"]

  def test_a_write_that_fails_is_raised_once_the_other_files_are_written(self, logs):
    (logs.db_path / "logs" / "demo-web.db").mkdir()

    with pytest.raises(OperationalError):
      logs.ingest("w1", push(event("demo-web", "lost"), event("demo-api", "kept"), event("traefik", access_line())))

    assert [row["message"] for row in logs.query_application_lines("demo-api")["lines"]] == ["kept"]
    assert logs._store("requests").count() == 1

  def test_an_applications_lines_keep_vectors_times_with_nine_digits_and_a_window_holds_the_lines_it_covers(self, logs):
    logs.ingest("w1", push({**event("demo-web", "tenth"), "timestamp": "2026-01-01T12:00:00.1Z"},
                           {**event("demo-web", "later"), "timestamp": "2026-01-01T12:00:00.123Z"}))

    assert [row["ts"] for row in logs.query_application_lines("demo-web")["lines"]] == [
        "2026-01-01T12:00:00.100000000Z", "2026-01-01T12:00:00.123000000Z"]
    window = logs.query_application_lines("demo-web", from_ts=datetime(2026, 1, 1, 12, 0, 0, 110000, tzinfo=timezone.utc))
    assert [row["message"] for row in window["lines"]] == ["later"]


class TestFeeds:
  def test_a_push_publishes_each_files_rows_as_their_routes_return_them(self, logs, published):
    logs.ingest("w1", push(event("demo-web", "one", 0), event("demo-web", "two", 1), event("traefik", access_line(2))))

    by_feed = {(feed, key): rows for feed, rows, key in published}
    assert by_feed == {(UpdateFeed.APPLICATION_LOGS, "demo-web"): logs.query_application_lines("demo-web")["lines"],
                       (UpdateFeed.REQUESTS, None): logs.query_requests()["requests"]}

  def test_a_file_that_could_not_be_written_publishes_nothing(self, logs, published):
    (logs.db_path / "logs" / "demo-web.db").mkdir()

    with pytest.raises(OperationalError):
      logs.ingest("w1", push(event("demo-web", "lost"), event("demo-api", "kept")))

    assert [(feed, key) for feed, _, key in published] == [(UpdateFeed.APPLICATION_LOGS, "demo-api")]
