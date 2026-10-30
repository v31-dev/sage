import logging
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from utils.logging import (ContextVarFilter, ExactPathFilter, SuppressTracebackFilter, TaskFailed, fastapi_exclude_log_paths, fastapi_middleware,
                           generate_task_id_token, get_log_level, sub_task_id, task_id)


def access_record(path, method="GET"):
  return logging.makeLogRecord({"msg": f'127.0.0.1:50000 - "{method} {path} HTTP/1.1" 200'})


class TestExactPathFilter:
  filter = ExactPathFilter(fastapi_exclude_log_paths)

  @pytest.mark.parametrize("path", [
      "/api",
      "/api?ts=1",
      "/api/vector/logs",
      "/api/workers/w1/logs/web",
      "/api/workers/w1/metrics",
      "/api/workers/w1/metrics?period=1h",
  ])
  def test_drops_the_noisy_requests(self, path):
    assert self.filter.filter(access_record(path)) is False

  @pytest.mark.parametrize("path", ["/api/", "/api/projects", "/api/workers/w1", "/api/workers/w1/reboot", "/"])
  def test_keeps_every_other_request(self, path):
    assert self.filter.filter(access_record(path)) is True

  def test_the_method_does_not_matter(self):
    assert self.filter.filter(access_record("/api/vector/logs", "POST")) is False

  def test_a_message_that_is_not_an_access_line_is_kept(self):
    assert self.filter.filter(logging.makeLogRecord({"msg": "Started server process"})) is True

  def test_patterns_are_matched_as_given(self):
    only_status = ExactPathFilter(["/status", "/files/*"])
    assert only_status.filter(access_record("/status")) is False
    assert only_status.filter(access_record("/files/a.txt")) is False
    assert only_status.filter(access_record("/status/extra")) is True


class TestSuppressTracebackFilter:
  def record(self, error):
    return logging.makeLogRecord({"msg": "x", "exc_info": (type(error), error, None), "exc_text": "Traceback ..."})

  def test_removes_the_traceback_of_a_failed_task(self):
    record = self.record(TaskFailed())
    assert SuppressTracebackFilter().filter(record) is True
    assert record.exc_info is None
    assert record.exc_text is None

  def test_keeps_the_traceback_of_any_other_error(self):
    record = self.record(ValueError("boom"))
    assert SuppressTracebackFilter().filter(record) is True
    assert record.exc_info[0] is ValueError
    assert record.exc_text == "Traceback ..."

  def test_a_record_without_an_error_passes(self):
    assert SuppressTracebackFilter().filter(logging.makeLogRecord({"msg": "x"})) is True


class TestContextVarFilter:
  def test_puts_the_task_id_on_the_record(self):
    token = task_id.set("abc12345")
    try:
      record = logging.makeLogRecord({"msg": "x"})
      assert ContextVarFilter().filter(record) is True
      assert record.task_id == "abc12345"
    finally:
      task_id.reset(token)

  def test_a_container_part_shows_its_task_and_its_own_id(self):
    tokens = task_id.set("abc12345"), sub_task_id.set("def67890")
    try:
      record = logging.makeLogRecord({"msg": "x"})
      ContextVarFilter().filter(record)
      assert record.task_id == "abc12345/def67890"
    finally:
      sub_task_id.reset(tokens[1])
      task_id.reset(tokens[0])

  def test_is_empty_outside_a_task(self):
    record = logging.makeLogRecord({"msg": "x"})
    ContextVarFilter().filter(record)
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
  tokens = {generate_task_id_token() for _ in range(50)}
  assert len(tokens) == 50
  assert all(re.fullmatch(r"[0-9a-f]{8}", token) for token in tokens)


class TestRequestMiddleware:
  @pytest.fixture
  def client(self):
    app = FastAPI()
    app.add_middleware(fastapi_middleware)

    @app.get("/ping")
    async def ping(request: Request):
      return {"state": request.state.task_id, "context": task_id.get()}

    @app.get("/sync-ping")
    def sync_ping():
      return {"context": task_id.get()}

    return TestClient(app)

  def test_every_response_carries_its_task_id(self, client):
    response = client.get("/ping")
    assert re.fullmatch(r"[0-9a-f]{8}", response.headers["X-Task-ID"])

  def test_the_task_id_reaches_the_handler_and_its_context(self, client):
    response = client.get("/ping")
    assert response.json() == {"state": response.headers["X-Task-ID"], "context": response.headers["X-Task-ID"]}

  def test_the_task_id_reaches_a_handler_that_runs_in_a_thread(self, client):
    response = client.get("/sync-ping")
    assert response.json()["context"] == response.headers["X-Task-ID"]

  def test_each_request_gets_its_own_task_id(self, client):
    assert client.get("/ping").headers["X-Task-ID"] != client.get("/ping").headers["X-Task-ID"]

  def test_the_request_is_logged_as_an_access_line(self, client, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
      client.get("/ping?x=1")
    messages = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    assert messages == ['testclient:50000 - "GET /ping?x=1 HTTP/1.1" 200']
