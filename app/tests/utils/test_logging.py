import logging
import re

import pytest

from utils.logging import (ContextVarFilter, ExactPathFilter, SuppressTracebackFilter, TaskFailed, fastapi_exclude_log_paths, get_log_level,
                           new_task_id, sub_task_id, task_id)


def access_record(path, method="GET"):
  return logging.makeLogRecord({"msg": f'127.0.0.1:50000 - "{method} {path} HTTP/1.1" 200'})


class TestExactPathFilter:
  filter = ExactPathFilter(fastapi_exclude_log_paths)

  @pytest.mark.parametrize("path", [
      "/api",
      "/api?ts=1",
      "/api/vector/logs",
      "/api/telegraf/report",
      "/api/projects/demo/applications/web/logs",
      "/api/requests/",
      "/api/tasks/abc12345/logs?sub_task_id=def67890",
      "/api/workers/w1/metrics",
      "/api/workers/w1/metrics?period=1h",
  ])
  def test_drops_the_noisy_requests(self, path):
    assert self.filter.filter(access_record(path)) is False

  @pytest.mark.parametrize("path", ["/api/", "/api/projects", "/api/workers/w1", "/api/workers/w1/reboot", "/api/tasks/history",
                                    "/"])
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
  tokens = {new_task_id() for _ in range(50)}
  assert len(tokens) == 50
  assert all(re.fullmatch(r"[0-9a-f]{8}", token) for token in tokens)
