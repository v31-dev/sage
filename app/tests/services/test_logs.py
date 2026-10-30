import asyncio
import itertools
import logging
import queue
import random
import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

import config
import services.logs as logs_module
from services.logs import Logs, _CaptureHandler
from utils.common import utcnow
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed, sub_task_id, task_id
from utils.queue import TaskQueue

log = logging.getLogger("tests.task")


def stamp(index):
  """A timestamp that sorts with `index`, in the form Docker gives (nanoseconds)."""
  return f"2026-01-{1 + index // 86400:02d}T{(index // 3600) % 24:02d}:{(index // 60) % 60:02d}:{index % 60:02d}.000000000Z"


def lines(first, count, host="w1", word="line"):
  return [{"hostname": host, "ts": stamp(index), "stream": "stdout",
           "message": f"{word}{index} GET /api/v1/items/{index} status=200 latency={index % 97}ms upstream=app"}
          for index in range(first, first + count)]


def task_entries(first, count, task="t1", sub=""):
  return [{"task_id": task, "sub_task_id": sub, "ts": stamp(index), "level": "INFO", "logger": "tests.task",
           "message": f"step {index} of the task, pulling and starting the containers"}
          for index in range(first, first + count)]


@pytest.fixture
def logs(data_dir):
  """The service without its capture, which would write from another thread."""
  logs = Logs()
  yield logs
  for shard in logs._dbs["logs"].values():
    shard["db"].close()


def rows(logs, name):
  shard = logs._store(name)
  return [row.ts for row in shard["lines"].select().order_by(shard["lines"].ts)]


def close_connections(logs):
  """Close the connections the calling thread opened: one to each file it used."""
  for shard in list(logs._dbs["logs"].values()):
    shard["db"].close()


def file_bytes(logs, container):
  """What the file takes on disk, once the write-ahead log is folded into it."""
  logs.get_logs_db(container)["db"].execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
  return (logs.db_path / "logs" / f"{container}.db").stat().st_size


@pytest.fixture
def small(monkeypatch):
  """Floors and batches a few thousand lines reach."""
  monkeypatch.setattr(config, "LOG_KEEP_NEWEST_ROWS", 50)
  monkeypatch.setattr(config, "LOG_TRIM_BATCH_ROWS", 500)


@pytest.fixture
def limit(monkeypatch):
  """Sets the size limits, after the test has written what it needs (a write checks the sizes)."""

  def set_limits(shard=None, total=None):
    if shard is not None:
      monkeypatch.setattr(config, "LOG_SHARD_MAX_BYTES", shard)
    if total is not None:
      monkeypatch.setattr(config, "LOG_TOTAL_MAX_BYTES", total)

  return set_limits


class TestStoring:
  def test_lines_come_back_oldest_first_and_can_be_filtered(self, logs):
    logs.write_logs("web", lines(0, 5, host="w1") + lines(5, 5, host="w2"))
    assert [row["ts"] for row in logs.query_logs("web")] == [stamp(i) for i in range(10)]
    assert {row["hostname"] for row in logs.query_logs("web", hostname="w2")} == {"w2"}
    assert [row["ts"] for row in logs.query_logs("web", from_ts=stamp(2), to_ts=stamp(4))] == [stamp(2), stamp(3)]

  def test_the_message_is_searchable(self, logs):
    logs.write_logs("web", lines(0, 20))
    assert [row["ts"] for row in logs.query_logs("web", search="line7")] == [stamp(7)]

  @pytest.mark.parametrize("name", ["", "../etc", "Web", "a b", "a/b", "tasks"])
  def test_a_container_name_that_could_leave_the_logs_directory_or_is_the_task_logs_is_refused(self, logs, name):
    with pytest.raises(ValueError):
      logs.get_logs_db(name)

  def test_the_timestamp_is_indexed(self, logs):
    shard = logs.get_logs_db("web")
    indexes = [row[1] for row in shard["db"].execute_sql("PRAGMA index_list('containerlogs')").fetchall()]
    assert "containerlogs_ts" in indexes

  def test_a_shard_made_before_the_index_gets_it_when_opened(self, logs):
    path = logs.db_path / "logs" / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE containerlogs (id INTEGER PRIMARY KEY AUTOINCREMENT, hostname VARCHAR(255) NOT NULL, "
                "ts VARCHAR(255) NOT NULL, stream VARCHAR(255) NOT NULL, message TEXT NOT NULL)")
    old.commit()
    old.close()
    shard = logs.get_logs_db("old")
    indexes = [row[1] for row in shard["db"].execute_sql("PRAGMA index_list('containerlogs')").fetchall()]
    assert "containerlogs_ts" in indexes


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
    assert tasks.logs._count(tasks.logs.task_logs_db()) == 3

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
    assert tasks.messages("t1", "t1") == whole, "a part recorded under the task's own id is the whole task"

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


class TestTrimShard:
  def test_a_shard_within_its_limit_is_left_alone(self, logs, small, limit):
    logs.write_logs("web", lines(0, 200))
    limit(shard=10**9)
    assert logs.trim_shard("web") == 0
    assert len(rows(logs, "web")) == 200

  def test_a_shard_over_its_limit_loses_its_oldest_lines_and_ends_below_it(self, logs, small, limit):
    logs.write_logs("web", lines(0, 3000))
    full = logs._live_bytes(logs.get_logs_db("web"))
    limit(shard=full // 2)
    removed = logs.trim_shard("web")
    left = rows(logs, "web")
    assert removed == 3000 - len(left) > 0
    assert left == [stamp(i) for i in range(removed, 3000)], "what is left is the newest lines, whole"
    live = logs._live_bytes(logs.get_logs_db("web"))
    assert 0.75 * config.LOG_SHARD_MAX_BYTES < live <= config.LOG_SHARD_MAX_BYTES

  def test_the_oldest_by_timestamp_go_first_not_the_first_written(self, logs, small, limit):
    logs.write_logs("web", lines(2000, 2000))
    logs.write_logs("web", lines(0, 1000))
    limit(shard=logs._live_bytes(logs.get_logs_db("web")) // 2)
    logs.trim_shard("web")
    left = rows(logs, "web")
    assert left[0] > stamp(1000), "history written later is the oldest, and goes first"
    assert left[-1] == stamp(3999)
    assert set(stamp(i) for i in range(3500, 4000)) <= set(left)

  def test_a_tiny_limit_still_leaves_the_newest_lines(self, logs, small, limit):
    logs.write_logs("web", lines(0, 1000))
    limit(shard=1000)
    logs.trim_shard("web")
    assert rows(logs, "web") == [stamp(i) for i in range(950, 1000)]

  def test_the_search_finds_only_the_lines_that_are_left(self, logs, small, limit):
    logs.write_logs("web", lines(0, 3000))
    limit(shard=logs._live_bytes(logs.get_logs_db("web")) // 2)
    removed = logs.trim_shard("web")
    assert logs.query_logs("web", search="line0") == []
    assert len(logs.query_logs("web", search=f"line{removed - 1}")) == 0
    assert len(logs.query_logs("web", search=f"line{removed + 5}")) == 1
    assert len(logs.query_logs("web", search="line2999")) == 1

  def test_trimming_twice_does_nothing_the_second_time(self, logs, small, limit):
    logs.write_logs("web", lines(0, 3000))
    limit(shard=logs._live_bytes(logs.get_logs_db("web")) // 2)
    assert logs.trim_shard("web") > 0
    assert logs.trim_shard("web") == 0

  def test_the_space_of_a_shard_far_bigger_than_its_limit_is_given_back(self, logs, small, limit):
    logs.write_logs("web", lines(0, 6000))
    big = file_bytes(logs, "web")
    shard = logs.get_logs_db("web")
    shard["lines"].delete().where(shard["lines"].ts < stamp(5500)).execute()
    shard["db"].execute_sql("INSERT INTO containerlogsindex(containerlogsindex) VALUES('optimize')")
    limit(shard=big // 3)
    assert logs._live_bytes(shard) < config.LOG_SHARD_MAX_BYTES < big / config.LOG_SHARD_VACUUM_RATIO
    assert logs.trim_shard("web") == 0
    assert file_bytes(logs, "web") < big / 2
    assert len(rows(logs, "web")) == 500

  def test_a_file_only_a_little_over_its_limit_is_not_rewritten(self, logs, small, limit):
    logs.write_logs("web", lines(0, 2000))
    shard = logs.get_logs_db("web")
    shard["lines"].delete().where(shard["lines"].ts < stamp(1500)).execute()
    before = file_bytes(logs, "web")
    limit(shard=before)
    assert logs.trim_shard("web") == 0
    assert file_bytes(logs, "web") == before


class TestSizeCheckAfterWrites:
  @pytest.fixture
  def clock(self, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(logs_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now

  def test_a_write_that_takes_a_shard_over_its_limit_trims_it(self, logs, small, limit, clock):
    limit(shard=300_000)
    logs.write_logs("web", lines(0, 100))
    clock[0] += config.LOG_SIZE_CHECK_SECONDS
    logs.write_logs("web", lines(100, 3000))
    assert len(rows(logs, "web")) < 3100
    assert rows(logs, "web")[-1] == stamp(3099)

  def test_the_check_is_made_at_most_once_in_the_interval(self, logs, small, limit, clock):
    limit(shard=300_000)
    logs.write_logs("web", lines(0, 100))
    logs.write_logs("web", lines(100, 3000))
    assert len(rows(logs, "web")) == 3100, "the second write came too soon after the check"
    clock[0] += config.LOG_SIZE_CHECK_SECONDS
    logs.write_logs("web", lines(3100, 10))
    assert len(rows(logs, "web")) < 3100

  def test_each_shard_has_its_own_interval(self, logs, small, limit, clock):
    limit(shard=300_000)
    logs.write_logs("web", lines(0, 100))
    logs.write_logs("api", lines(0, 3000))
    assert len(rows(logs, "api")) < 3000, "the first write to a shard is checked at once"

  def test_the_task_logs_are_trimmed_too(self, logs, small, limit, clock):
    limit(shard=300_000)
    for entry in task_entries(0, 3000):
      logs.write_task_line(entry)
    assert len(rows(logs, "tasks")) == 3000
    clock[0] += config.LOG_SIZE_CHECK_SECONDS
    logs.write_task_line(task_entries(3000, 1)[0])
    left = rows(logs, "tasks")
    assert len(left) < 3000 and left[-1] == stamp(3000)

  def test_an_error_while_trimming_never_fails_the_write(self, logs, small, clock, monkeypatch, caplog):
    def broken(container):
      raise RuntimeError("disk is gone")

    monkeypatch.setattr(logs, "trim_shard", broken)
    with caplog.at_level(logging.WARNING):
      logs.write_logs("web", lines(0, 10))
    assert len(rows(logs, "web")) == 10
    assert any("Could not trim" in record.getMessage() and record.exc_info for record in caplog.records)

  def test_a_trim_already_under_way_is_not_started_again(self, logs, small, clock, monkeypatch):
    called = []
    monkeypatch.setattr(logs, "trim_shard", lambda container: called.append(container))
    logs._trim_lock.acquire()
    try:
      logs.write_logs("web", lines(0, 10))
    finally:
      logs._trim_lock.release()
    assert called == []


class TestTrimTotal:
  def fill(self, logs):
    logs.write_logs("old-app", lines(0, 1500, word="old"))
    logs.write_logs("busy-app", lines(1000, 1500, word="busy"))
    logs.write_logs("new-app", lines(2000, 1500, word="new"))

  def total(self, logs):
    return sum(logs._live_bytes(logs.get_logs_db(name)) for name in ("old-app", "busy-app", "new-app"))

  def test_all_the_logs_within_the_limit_are_left_alone(self, logs, small, limit):
    self.fill(logs)
    limit(total=self.total(logs) * 2)
    assert logs.trim_total() == 0

  def test_over_the_limit_the_oldest_lines_of_any_container_go_first(self, logs, small, limit, monkeypatch):
    self.fill(logs)
    limit(total=self.total(logs) // 2)
    assert logs.trim_total() > 0
    assert self.total(logs) <= config.LOG_TOTAL_MAX_BYTES
    old, busy, new = (rows(logs, name) for name in ("old-app", "busy-app", "new-app"))
    assert len(old) == config.LOG_KEEP_NEWEST_ROWS, "the container with the oldest lines is down to its newest"
    assert 0 < len(busy) < 1500
    assert len(new) == 1500

  def test_nothing_is_removed_while_an_older_line_is_kept_elsewhere(self, logs, small, limit, monkeypatch):
    self.fill(logs)
    limit(total=self.total(logs) // 2)
    logs.trim_total()
    busy, new = rows(logs, "busy-app"), rows(logs, "new-app")
    removed = {stamp(i) for i in range(1000, 2500)} - set(busy)
    older_kept = [ts for ts in busy + new if ts < max(removed)]
    assert removed and len(older_kept) <= config.LOG_TRIM_BATCH_ROWS, "newer lines went before older ones, by more than a batch"

  def test_every_container_keeps_its_newest_lines_whatever_the_limit(self, logs, small, limit, monkeypatch):
    self.fill(logs)
    limit(total=1000)
    logs.trim_total()
    for name, last in (("old-app", 1499), ("busy-app", 2499), ("new-app", 3499)):
      assert rows(logs, name)[-config.LOG_KEEP_NEWEST_ROWS:] == [stamp(i) for i in range(last - config.LOG_KEEP_NEWEST_ROWS + 1, last + 1)]
      assert len(rows(logs, name)) == config.LOG_KEEP_NEWEST_ROWS

  def test_the_limit_is_the_smaller_of_the_size_and_a_share_of_the_disk(self, logs, monkeypatch):
    monkeypatch.setattr(config, "LOG_TOTAL_MAX_BYTES", 2 * 2**30)
    monkeypatch.setattr(logs_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=100 * 2**30))
    assert logs.total_limit() == 2 * 2**30
    monkeypatch.setattr(logs_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=10 * 2**30))
    assert logs.total_limit() == 2**30

  def test_the_task_logs_count_and_lose_their_oldest_lines_like_a_container(self, logs, small, limit):
    self.fill(logs)
    for entry in task_entries(0, 1500):
      logs.write_task_line(entry)
    limit(total=(self.total(logs) + logs._live_bytes(logs.task_logs_db())) // 2)
    assert logs.trim_total() > 0
    assert config.LOG_KEEP_NEWEST_ROWS <= len(rows(logs, "tasks")) < 1500
    assert len(rows(logs, "new-app")) == 1500

  def test_the_total_is_checked_after_a_write(self, logs, small, limit, monkeypatch):
    self.fill(logs)
    limit(total=self.total(logs) // 2)
    monkeypatch.setattr(logs, "_total_checked", float("-inf"))
    logs.write_logs("new-app", lines(3500, 10, word="new"))
    assert self.total(logs) <= config.LOG_TOTAL_MAX_BYTES * 1.05


class TestCleanup:
  def test_lines_past_the_retention_and_the_shards_of_removed_applications_go(self, logs, application):
    kept = application.qualified_name
    old = (utcnow() - timedelta(days=config.RETENTION_DAYS + 1)).isoformat()
    fresh = utcnow().isoformat()
    logs.write_logs(kept, [{"hostname": "w1", "ts": old, "stream": "stdout", "message": "stale"},
                           {"hostname": "w1", "ts": fresh, "stream": "stdout", "message": "recent"}])
    logs.write_logs("deleted-app", lines(0, 5))
    logs.write_logs("sage", lines(0, 5))
    logs.write_logs("traefik", lines(0, 5))
    logs.cleanup()
    assert [row["message"] for row in logs.query_logs(kept)] == ["recent"]
    assert not (logs.db_path / "logs" / "deleted-app.db").exists()
    assert not (logs.db_path / "logs" / "sage.db").exists()
    assert (logs.db_path / "logs" / "traefik.db").exists()

  def test_the_task_logs_are_kept_and_lose_their_lines_past_the_retention(self, logs, db):
    old = (utcnow() - timedelta(days=config.RETENTION_DAYS + 1)).isoformat()
    logs.write_task_line({**task_entries(0, 1)[0], "ts": old, "message": "stale"})
    logs.write_task_line({**task_entries(1, 1)[0], "ts": utcnow().isoformat(), "message": "recent"})
    logs.cleanup()
    assert [line["message"] for line in logs.query_task_logs("t1")["lines"]] == ["recent"]

  def test_it_applies_the_size_limits(self, logs, application, small, limit):
    name = application.qualified_name
    logs.write_logs(name, lines(0, 3000))
    logs.write_logs("traefik", lines(0, 3000))
    limit(shard=logs._live_bytes(logs.get_logs_db("traefik")) // 2)
    logs.cleanup(days=36500)
    assert 0 < len(rows(logs, name)) < 3000
    assert 0 < len(rows(logs, "traefik")) < 3000


class TestUnderLoad:
  def test_writers_on_several_threads_with_trims_in_between_leave_every_shard_whole(self, logs, small, limit, monkeypatch):
    monkeypatch.setattr(config, "LOG_SIZE_CHECK_SECONDS", 0)
    limit(shard=150_000, total=400_000)
    containers = ["web", "api", "traefik", "worker"]
    counter = itertools.count()
    newest = {}
    guard = threading.Lock()
    errors = []

    def next_lines(rng, container):
      with guard:
        batch = [{"hostname": "w1", "ts": stamp(next(counter)), "stream": "stdout",
                  "message": f"{container} request {rng.randint(0, 10**6)} path=/a/b/{rng.randint(0, 999)} " + "x" * rng.randint(0, 120)}
                 for _ in range(rng.randint(20, 80))]
        newest[container] = max(newest.get(container, ""), batch[-1]["ts"])
      return batch

    def writer(seed):
      rng = random.Random(seed)
      try:
        for _ in range(60):
          container = rng.choice(containers)
          logs.write_logs(container, next_lines(rng, container))
      except Exception as error:
        errors.append(traceback.format_exc())
      finally:
        close_connections(logs)

    def task_logger():
      rng = random.Random(99)
      try:
        for _ in range(25):
          for entry in next_lines(rng, "tasks"):
            logs.write_task_line({"task_id": "t1", "sub_task_id": "", "ts": entry["ts"], "level": "INFO",
                                  "logger": "tests.task", "message": entry["message"]})
      except Exception as error:
        errors.append(traceback.format_exc())
      finally:
        close_connections(logs)

    workers = [threading.Thread(target=writer, args=(seed,)) for seed in range(4)] + [threading.Thread(target=task_logger)]
    for worker in workers:
      worker.start()
    for worker in workers:
      worker.join()
    assert errors == [], errors[0]

    logs.trim_total()
    for name in containers + ["tasks"]:
      shard = logs._store(name)
      assert shard["db"].execute_sql("PRAGMA integrity_check").fetchone()[0] == "ok"
      if shard["index"] is not None:
        shard["db"].execute_sql("INSERT INTO containerlogsindex(containerlogsindex, rank) VALUES('integrity-check', 1)")
      assert logs._live_bytes(shard) <= config.LOG_SHARD_MAX_BYTES, name
      assert rows(logs, name)[-1] == newest[name], f"the newest line of {name} was trimmed"
      assert len(rows(logs, name)) >= config.LOG_KEEP_NEWEST_ROWS
    assert sum(logs._live_bytes(logs._store(name)) for name in containers + ["tasks"]) <= config.LOG_TOTAL_MAX_BYTES

  def test_a_trim_while_another_thread_writes_the_same_shard(self, logs, small, limit, monkeypatch):
    monkeypatch.setattr(config, "LOG_SIZE_CHECK_SECONDS", 10**9)
    monkeypatch.setattr(config, "LOG_TRIM_BATCH_ROWS", 10)
    logs.write_logs("web", lines(0, 400))
    errors = []

    def write():
      try:
        for index in range(400):
          logs.write_logs("web", lines(400 + index * 5, 5))
      except Exception:
        errors.append(traceback.format_exc())
      finally:
        close_connections(logs)

    writer = threading.Thread(target=write)
    writer.start()
    try:
      while writer.is_alive():
        limit(shard=logs._live_bytes(logs.get_logs_db("web")) * 3 // 4)
        logs.trim_shard("web")
    finally:
      writer.join()
    assert errors == [], errors[0] if errors else ""
    assert logs.get_logs_db("web")["db"].execute_sql("PRAGMA integrity_check").fetchone()[0] == "ok"
