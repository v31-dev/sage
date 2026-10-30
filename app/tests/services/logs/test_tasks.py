import asyncio
import logging
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import config
from services.logs.tasks import _CaptureHandler
from services.updates import UpdateFeed
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed, sub_task_id, task_id
from utils.queue import TaskQueue

from .builders import stamp, task_entries

log = logging.getLogger("tests.task")


class Tasks:
  """A queue whose tasks' lines are captured as the service captures them, and written once the tasks are done."""

  def __init__(self, logs):
    self.logs = logs
    self.captured = queue.SimpleQueue()
    self.handler = _CaptureHandler(self.captured)
    self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lane")
    self.queue = TaskQueue(frozenset(["app"]), {"app": self.pool}, record=lambda task, status: None, notify=lambda: None)

  def add(self, task, scope="app:a", **kwargs):
    return self.queue.add_task(task=task, scopes={scope}, executor="app", **kwargs)

  async def run(self):
    self.queue.dispatch_tick()
    while self.queue.snapshot()["running"]:
      await asyncio.sleep(0)
      self.queue.dispatch_tick()
    while not self.captured.empty():
      entry = self.captured.get()
      self.logs.write_task_line(entry)

  def messages(self, task_id, sub_task_id=""):
    return [line["message"] for line in self.logs.query_task_logs(task_id, sub_task_id)["lines"]]


@pytest.fixture
def tasks(logs, caplog):
  caplog.set_level(logging.INFO)
  tasks = Tasks(logs)
  logging.getLogger().addHandler(tasks.handler)
  yield tasks
  logging.getLogger().removeHandler(tasks.handler)
  tasks.pool.shutdown(wait=False, cancel_futures=True)


class TestTaskLines:
  async def test_a_task_lines_are_stored_under_its_id_in_order(self, tasks):
    async def deploy():
      log.info("pulling the image")
      log.warning("the image is old")

    tasks.add(deploy, task_id="t1")
    await tasks.run()
    assert [(line["level"], line["logger"], line["message"]) for line in tasks.logs.query_task_logs("t1")["lines"]] == [
        ("INFO", "utils.queue", "deploy: started"),
        ("INFO", "tests.task", "pulling the image"),
        ("WARNING", "tests.task", "the image is old"),
        ("INFO", "utils.queue", "deploy: completed"),
    ]

  async def test_the_request_lines_before_it_queues_the_task_are_not_the_task_lines(self, tasks):
    async def deploy():
      log.info("deploying")

    token = task_id.set("t1")
    try:
      log.info("validating the request")
      tasks.add(deploy, task_id="t1")
    finally:
      task_id.reset(token)
    log.info("outside any request or task")
    await tasks.run()
    assert tasks.messages("t1") == ["deploy: started", "deploying", "deploy: completed"]
    assert tasks.logs._store("tasks").count() == 3

  async def test_a_sync_task_and_what_a_task_runs_in_a_thread_are_its_lines(self, tasks):
    def build():
      log.info("built")

    async def deploy():
      await run_in_executor_with_context(lambda: log.info("offloaded"))

    tasks.add(build, task_id="t1")
    tasks.add(deploy, scope="app:b", task_id="t2")
    await tasks.run()
    assert tasks.messages("t1") == ["build: started", "built", "build: completed"]
    assert tasks.messages("t2") == ["deploy: started", "offloaded", "deploy: completed"]

  async def test_tasks_running_together_keep_their_lines_apart(self, tasks):
    async def chatty(word):
      for index in range(5):
        log.info(f"{word} {index}")
        await asyncio.sleep(0)

    tasks.add(chatty, scope="app:a", params={"word": "a"}, task_id="ta")
    tasks.add(chatty, scope="app:b", params={"word": "b"}, task_id="tb")
    await tasks.run()
    for word in ("a", "b"):
      assert tasks.messages(f"t{word}") == ["chatty: started", *(f"{word} {index}" for index in range(5)), "chatty: completed"]

  async def test_a_container_part_is_read_with_its_task_or_on_its_own(self, tasks):
    async def deploy():
      log.info("deploying two containers")
      for part in ("p1", "p2"):
        token = sub_task_id.set(part)
        try:
          log.info(f"container {part}")
        finally:
          sub_task_id.reset(token)

    tasks.add(deploy, task_id="t1")
    await tasks.run()
    whole = ["deploy: started", "deploying two containers", "container p1", "container p2", "deploy: completed"]
    assert tasks.messages("t1") == whole
    assert tasks.messages("t1", "p2") == ["container p2"]

  async def test_a_quiet_task_that_succeeds_leaves_no_lines(self, tasks):
    async def sync():
      log.info("nothing changed")

    tasks.add(sync, task_id="q1", quiet=True)
    await tasks.run()
    assert tasks.messages("q1") == []

  @pytest.mark.parametrize("quiet", [True, False])
  async def test_a_task_that_fails_keeps_each_of_its_lines_once(self, tasks, quiet):
    async def sync():
      log.info("checking w1")
      raise TaskFailed()

    tasks.add(sync, task_id="q1", quiet=quiet)
    await tasks.run()
    assert tasks.messages("q1") == ["sync: started", "checking w1", "sync: failed"]

  def test_a_line_that_cannot_be_written_is_a_warning_and_the_next_one_is_written(self, logs, monkeypatch, caplog):
    written = threading.Event()

    def write(entry):
      if entry["message"] == "disk full":
        raise OSError(entry["message"])
      written.set()

    monkeypatch.setattr(logs, "write_task_line", write)
    threading.Thread(target=logs._capture_loop, daemon=True).start()
    for message in ("disk full", "fine"):
      logs._captured.put({"task_id": "t1", "message": message})

    assert written.wait(timeout=5)
    assert "Could not write a line of task t1." in caplog.text

  async def test_a_crash_keeps_its_traceback_in_the_message(self, tasks):
    async def sync():
      raise RuntimeError("boom")

    tasks.add(sync, task_id="q1", quiet=True)
    await tasks.run()
    failed = tasks.messages("q1")[-1]
    assert failed.startswith("sync: failed\nTraceback") and "RuntimeError: boom" in failed

  def test_a_query_gives_the_newest_lines_and_says_when_there_were_more(self, logs, monkeypatch):
    monkeypatch.setattr(config, "LOG_QUERY_MAX_ROWS", 5)
    for entry in task_entries(0, 5):
      logs.write_task_line(entry)
    assert logs.query_task_logs("t1")["more"] is False
    for entry in task_entries(5, 2):
      logs.write_task_line(entry)
    result = logs.query_task_logs("t1")
    assert [line["ts"] for line in result["lines"]] == [stamp(index) for index in range(2, 7)]
    assert result["more"] is True

  async def test_a_task_line_is_kept_with_nine_digits(self, tasks):
    async def deploy():
      log.info("deploying")

    tasks.add(deploy, task_id="t1")
    await tasks.run()
    stored = [line["ts"] for line in tasks.logs.query_task_logs("t1")["lines"]]
    assert stored and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z", ts) for ts in stored)


class TestFeeds:
  def test_a_task_line_goes_to_its_tasks_feed_and_a_parts_line_to_its_parts_too(self, logs, published):
    logs.write_task_line(task_entries(0, 1, task="t1")[0])
    logs.write_task_line(task_entries(1, 1, task="t1", sub="c1")[0])

    assert [(feed, key, [row["ts"] for row in rows]) for feed, rows, key in published] == [
        (UpdateFeed.TASK_LOGS, "t1", [stamp(0)]), (UpdateFeed.TASK_LOGS, "t1", [stamp(1)]),
        (UpdateFeed.TASK_LOGS, "t1/c1", [stamp(1)])]

  def test_a_published_task_line_is_the_line_its_route_returns(self, logs, published):
    logs.write_task_line(task_entries(0, 1)[0])

    [(_, rows, _)] = published
    assert rows == logs.query_task_logs("t1")["lines"]


class TestAfter:
  """What a view loads when the socket connects again: the rows written after the last one it holds."""

  def test_a_tasks_lines(self, logs):
    for entry in task_entries(0, 4):
      logs.write_task_line(entry)
    held = logs.query_task_logs("t1")["lines"][1]["id"]
    assert [line["ts"] for line in logs.query_task_logs("t1", after_id=held)["lines"]] == [stamp(2), stamp(3)]
