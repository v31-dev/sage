import logging
import re

import pytest
from fastapi import APIRouter, FastAPI

from utils.logging import NO_ACCESS_LOG, AccessLogFilter, TaskIdFilter, get_log_level, new_task_id, sub_task_id, task_id, unlogged_paths


def access_record(path, method="GET"):
  """A line of uvicorn's access log, as it logs it."""
  return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                           ("127.0.0.1:50000", method, path, "1.1", 200), None)


@pytest.fixture(scope="module")
def apps_filter():
  """The access log's filter as Sage sets it up: its two apps' marked routes, and the Socket.IO path."""
  from api import SOCKET_IO_PATH, app
  from api_ingest import app as ingest
  return AccessLogFilter(unlogged_paths([app, ingest], [SOCKET_IO_PATH]))


class TestTheAppsUnloggedPaths:

  @pytest.mark.parametrize("path", [
      "/api",
      "/api?ts=1",
      "/api/vector/logs",
      "/api/telegraf/report",
      "/api/projects/demo/applications/web/logs",
      "/api/projects/demo/applications/web/logs?hostname=w1&search=a%20b",
      "/api/requests/",
      "/api/tasks/abc12345/logs?sub_task_id=def67890",
      "/api/workers/w1/metrics",
      "/api/workers/w1/metrics?period=1h",
      "/api/socket.io/?EIO=4&transport=polling",
  ])
  def test_leave_out_the_requests_asked_every_few_seconds(self, apps_filter, path):
    assert apps_filter.filter(access_record(path)) is False

  @pytest.mark.parametrize("path", [
      "/",
      "/api/tasks/",
      "/api/tasks/abc12345/parts",
      "/api/workers/w1",
      "/api/projects/demo/applications/web",
      "/api/projects/demo/applications/web/logs/extra",
      "/api/socket.iox",
  ])
  def test_keep_every_other_request(self, apps_filter, path):
    assert apps_filter.filter(access_record(path)) is True


class TestAccessLogFilter:
  def app(self, prefix):
    """A router with a marked route and one that is not, in another router, as the projects hold the applications."""
    inner = APIRouter()

    @inner.get("/{name}/logs", openapi_extra=NO_ACCESS_LOG)
    def logs(name: str):
      return []

    @inner.get("/{name}")
    def one(name: str):
      return {}

    outer = APIRouter()
    outer.include_router(inner, prefix="/{group}/things")
    app = FastAPI(openapi_url=None)
    app.include_router(outer, prefix=prefix)
    return app

  def test_a_marked_route_is_left_out_wherever_it_is_included(self):
    for prefix in ("/api", "/api/v2"):
      filter = AccessLogFilter(unlogged_paths([self.app(prefix)]))
      assert filter.filter(access_record(f"{prefix}/g/things/a/logs")) is False
      assert filter.filter(access_record(f"{prefix}/g/things/a")) is True

  def test_the_method_does_not_matter(self):
    filter = AccessLogFilter(unlogged_paths([self.app("/api")]))
    assert filter.filter(access_record("/api/g/things/a/logs", method="POST")) is False

  def test_a_record_that_is_not_an_access_line_is_kept(self):
    filter = AccessLogFilter(unlogged_paths([self.app("/api")]))
    assert filter.filter(logging.makeLogRecord({"msg": "GET /api/g/things/a/logs"})) is True

  def test_a_path_under_a_prefix_is_left_out_and_the_prefix_itself(self):
    filter = AccessLogFilter(unlogged_paths([], ["/api/socket.io"]))
    assert [filter.filter(access_record(path)) for path in ("/api/socket.io", "/api/socket.io/", "/api/socket.io/x", "/api/socket")] == [
        False, False, False, True]


class TestTaskIdFilter:
  def test_puts_the_task_id_on_the_record(self):
    token = task_id.set("abc12345")
    try:
      record = logging.makeLogRecord({"msg": "x"})
      assert TaskIdFilter().filter(record) is True
      assert record.task_id == "abc12345"
    finally:
      task_id.reset(token)

  def test_a_container_part_shows_its_task_and_its_own_id(self):
    tokens = task_id.set("abc12345"), sub_task_id.set("def67890")
    try:
      record = logging.makeLogRecord({"msg": "x"})
      TaskIdFilter().filter(record)
      assert record.task_id == "abc12345/def67890"
    finally:
      sub_task_id.reset(tokens[1])
      task_id.reset(tokens[0])

  def test_is_empty_outside_a_task(self):
    record = logging.makeLogRecord({"msg": "x"})
    TaskIdFilter().filter(record)
    assert record.task_id == ""


class TestGetLogLevel:
  @pytest.mark.parametrize("value, level", [
      ("DEBUG", logging.DEBUG),
      ("debug", logging.DEBUG),
      ("  warning  ", logging.WARNING),
      ("Error", logging.ERROR),
      ("INFO", logging.INFO),
  ])
  def test_reads_the_level_name(self, monkeypatch, value, level):
    monkeypatch.setenv("LOG_LEVEL", value)
    assert get_log_level() == level

  @pytest.mark.parametrize("value", [None, "", "   ", "loud", "42"])
  def test_anything_else_means_info(self, monkeypatch, value):
    if value is None:
      monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
      monkeypatch.setenv("LOG_LEVEL", value)
    assert get_log_level() == logging.INFO


def test_task_ids_are_eight_hex_characters_and_differ():
  tokens = {new_task_id() for _ in range(50)}
  assert len(tokens) == 50
  assert all(re.fullmatch(r"[0-9a-f]{8}", token) for token in tokens)
