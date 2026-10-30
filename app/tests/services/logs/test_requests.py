from datetime import datetime, timezone

import pytest

import config

from .builders import access_line, event, moment, push, request_at, stamp


def requested(logs, **filters):
  return [row["path"] for row in logs.query_requests(**filters)["requests"]]


class TestRequests:
  @pytest.fixture
  def stored(self, logs):
    logs.ingest("w1", push(request_at(0, RequestHost="web.example.com", RequestPath="/a", DownstreamStatus=200,
                                      Duration=5_000_000),
                           request_at(1, RequestHost="API.example.com", RequestPath="/b?q=100%",
                                      RequestMethod="POST", DownstreamStatus=201, Duration=250_000_000),
                           request_at(2, RequestHost="web.example.com", RequestPath="/c_d", DownstreamStatus=404,
                                      Duration=1_000_000)))
    logs.ingest("w2", push(request_at(3, RequestHost="web.example.com", RequestPath="/e", DownstreamStatus=503,
                                      Duration=2_000_000_000)))
    return logs

  def test_they_come_newest_first(self, stored):
    assert requested(stored) == ["/e", "/c_d", "/b?q=100%", "/a"]

  @pytest.mark.parametrize("filters, found", [
      ({"worker": "w2"}, ["/e"]),
      ({"host": "api.EXAMPLE"}, ["/b?q=100%"]),
      ({"method": "POST"}, ["/b?q=100%"]),
      ({"status_class": 2}, ["/b?q=100%", "/a"]),
      ({"status_class": 4}, ["/c_d"]),
      ({"status_class": 5}, ["/e"]),
      ({"path": "%"}, ["/b?q=100%"]),
      ({"path": "_"}, ["/c_d"]),
      ({"min_duration_ms": 250}, ["/e", "/b?q=100%"]),
      ({"from_ts": moment(1), "to_ts": moment(3)}, ["/c_d", "/b?q=100%"]),
      ({"host": "web", "status_class": 4}, ["/c_d"]),
  ])
  def test_each_filter_narrows_them(self, stored, filters, found):
    assert requested(stored, **filters) == found

  def test_a_query_gives_the_newest_and_says_when_there_were_more(self, logs, monkeypatch):
    monkeypatch.setattr(config, "LOG_QUERY_MAX_ROWS", 3)
    logs.ingest("w1", push(*(request_at(index) for index in range(4))))
    result = logs.query_requests()
    assert [row["ts"] for row in result["requests"]] == [stamp(3), stamp(2), stamp(1)]
    assert result["more"] is True

  @pytest.mark.parametrize("given, kept", [
      ("2026-01-01T12:00:00.1Z", "2026-01-01T12:00:00.100000000Z"),
      ("2026-01-01T12:00:00Z", "2026-01-01T12:00:00.000000000Z"),
      ("2026-01-01T12:00:00.123456789Z", "2026-01-01T12:00:00.123456789Z"),
  ])
  def test_a_start_is_kept_with_nine_digits_so_that_times_compare_as_text(self, logs, given, kept):
    logs.ingest("w1", push(event("traefik", access_line(StartUTC=given))))
    assert [row["ts"] for row in logs.query_requests()["requests"]] == [kept]

  def test_a_window_holds_the_starts_it_covers_whatever_their_precision(self, logs):
    logs.ingest("w1", push(event("traefik", access_line(StartUTC="2026-01-01T12:00:00.1Z", RequestPath="/tenth")),
                           event("traefik", access_line(StartUTC="2026-01-01T12:00:00.123Z", RequestPath="/later"))))
    assert requested(logs, from_ts=datetime(2026, 1, 1, 12, 0, 0, 110000, tzinfo=timezone.utc)) == ["/later"]


class TestAfter:
  """What a view loads when the socket connects again: the rows written after the last one it holds."""

  def test_the_requests(self, logs):
    logs.ingest("w1", push(*(request_at(index) for index in range(4))))
    held = logs.query_requests()["requests"][2]["id"]
    assert [row["ts"] for row in logs.query_requests(after_id=held)["requests"]] == [stamp(3), stamp(2)]
