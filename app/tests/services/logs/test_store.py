import itertools
import logging
import random
import threading
import traceback
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import services.logs.store as store_module
from services.logs.store import stored_time
from utils.common import utcnow

from .builders import access_line, event, lines, push, stamp, task_entries


def rows(logs, name):
  shard = logs._store(name)
  return [row.ts for row in shard.lines.select().order_by(shard.lines.ts)]


def close_connections(logs):
  """Close the connections the calling thread opened: one to each file it used."""
  for shard in list(logs._stores.values()):
    shard.db.close()


def file_bytes(logs, container):
  """What the file takes on disk, once the write-ahead log is folded into it."""
  logs.application_store(container).db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE)")
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


class TestStoredTime:
  @pytest.mark.parametrize("given, kept", [
      ("2026-01-01T12:00:00.1Z", "2026-01-01T12:00:00.100000000Z"),
      ("2026-01-01T12:00:00Z", "2026-01-01T12:00:00.000000000Z"),
      ("2026-01-01T12:00:00.123456789Z", "2026-01-01T12:00:00.123456789Z"),
      (datetime(2026, 1, 1, 12, 0, 0, 500000), "2026-01-01T12:00:00.500000000Z"),
      (datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))), "2026-01-01T12:00:00.000000000Z"),
      ("not a time", "not a time"),
  ])
  def test_is_utc_with_nine_digits_of_fraction(self, given, kept):
    assert stored_time(given) == kept


class TestTrimShard:
  def test_a_shard_within_its_limit_is_left_alone(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 200))
    limit(shard=10**9)
    assert logs.trim_shard("web") == 0
    assert len(rows(logs, "web")) == 200

  def test_a_shard_over_its_limit_loses_its_oldest_lines_and_ends_below_it(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 3000))
    full = logs.application_store("web").live_bytes()
    limit(shard=full // 2)
    removed = logs.trim_shard("web")
    left = rows(logs, "web")
    assert removed == 3000 - len(left) > 0
    assert left == [stamp(i) for i in range(removed, 3000)], "what is left is the newest lines, whole"
    live = logs.application_store("web").live_bytes()
    assert 0.75 * config.LOG_SHARD_MAX_BYTES < live <= config.LOG_SHARD_MAX_BYTES

  def test_the_oldest_by_timestamp_go_first_not_the_first_written(self, logs, small, limit):
    logs.write_application_lines("web", lines(2000, 2000))
    logs.write_application_lines("web", lines(0, 1000))
    limit(shard=logs.application_store("web").live_bytes() // 2)
    logs.trim_shard("web")
    left = rows(logs, "web")
    assert left[0] > stamp(1000), "history written later is the oldest, and goes first"
    assert left[-1] == stamp(3999)
    assert set(stamp(i) for i in range(3500, 4000)) <= set(left)

  def test_a_tiny_limit_still_leaves_the_newest_lines(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 1000))
    limit(shard=1000)
    logs.trim_shard("web")
    assert rows(logs, "web") == [stamp(i) for i in range(950, 1000)]

  def test_the_search_finds_only_the_lines_that_are_left(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 3000))
    limit(shard=logs.application_store("web").live_bytes() // 2)
    removed = logs.trim_shard("web")
    assert logs.query_application_lines("web", search="line0")["lines"] == []
    assert len(logs.query_application_lines("web", search=f"line{removed - 1}")["lines"]) == 0
    assert len(logs.query_application_lines("web", search=f"line{removed + 5}")["lines"]) == 1
    assert len(logs.query_application_lines("web", search="line2999")["lines"]) == 1

  def test_trimming_twice_does_nothing_the_second_time(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 3000))
    limit(shard=logs.application_store("web").live_bytes() // 2)
    assert logs.trim_shard("web") > 0
    assert logs.trim_shard("web") == 0

  def test_the_space_of_a_shard_far_bigger_than_its_limit_is_given_back(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 6000))
    big = file_bytes(logs, "web")
    shard = logs.application_store("web")
    shard.lines.delete().where(shard.lines.ts < stamp(5500)).execute()
    shard.db.execute_sql("INSERT INTO containerlogsindex(containerlogsindex) VALUES('optimize')")
    limit(shard=big // 3)
    assert shard.live_bytes() < config.LOG_SHARD_MAX_BYTES < big / config.LOG_SHARD_VACUUM_RATIO
    assert logs.trim_shard("web") == 0
    assert file_bytes(logs, "web") < big / 2
    assert len(rows(logs, "web")) == 500

  def test_a_file_only_a_little_over_its_limit_is_not_rewritten(self, logs, small, limit):
    logs.write_application_lines("web", lines(0, 2000))
    shard = logs.application_store("web")
    shard.lines.delete().where(shard.lines.ts < stamp(1500)).execute()
    before = file_bytes(logs, "web")
    limit(shard=before)
    assert logs.trim_shard("web") == 0
    assert file_bytes(logs, "web") == before


class TestSizeCheckAfterWrites:
  @pytest.fixture
  def clock(self, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(store_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now

  def test_a_write_that_takes_a_shard_over_its_limit_trims_it(self, logs, small, limit, clock):
    limit(shard=300_000)
    logs.write_application_lines("web", lines(0, 100))
    clock[0] += config.LOG_SIZE_CHECK_SECONDS
    logs.write_application_lines("web", lines(100, 3000))
    assert len(rows(logs, "web")) < 3100
    assert rows(logs, "web")[-1] == stamp(3099)

  def test_the_check_is_made_at_most_once_in_the_interval(self, logs, small, limit, clock):
    limit(shard=300_000)
    logs.write_application_lines("web", lines(0, 100))
    logs.write_application_lines("web", lines(100, 3000))
    assert len(rows(logs, "web")) == 3100, "the second write came too soon after the check"
    clock[0] += config.LOG_SIZE_CHECK_SECONDS
    logs.write_application_lines("web", lines(3100, 10))
    assert len(rows(logs, "web")) < 3100

  def test_each_shard_has_its_own_interval(self, logs, small, limit, clock):
    limit(shard=300_000)
    logs.write_application_lines("web", lines(0, 100))
    logs.write_application_lines("api", lines(0, 3000))
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
      logs.write_application_lines("web", lines(0, 10))
    assert len(rows(logs, "web")) == 10
    assert any("Could not trim" in record.getMessage() and record.exc_info for record in caplog.records)

  def test_a_trim_already_under_way_is_not_started_again(self, logs, small, clock, monkeypatch):
    called = []
    monkeypatch.setattr(logs, "trim_shard", lambda container: called.append(container))
    logs._trim_lock.acquire()
    try:
      logs.write_application_lines("web", lines(0, 10))
    finally:
      logs._trim_lock.release()
    assert called == []


class TestTrimTotal:
  def fill(self, logs):
    logs.write_application_lines("old-app", lines(0, 1500, word="old"))
    logs.write_application_lines("busy-app", lines(1000, 1500, word="busy"))
    logs.write_application_lines("new-app", lines(2000, 1500, word="new"))

  def total(self, logs):
    return sum(logs.application_store(name).live_bytes() for name in ("old-app", "busy-app", "new-app"))

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
    monkeypatch.setattr(store_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=100 * 2**30))
    assert logs.total_limit() == 2 * 2**30
    monkeypatch.setattr(store_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=10 * 2**30))
    assert logs.total_limit() == 2**30

  def test_the_task_logs_count_and_lose_their_oldest_lines_like_a_container(self, logs, small, limit):
    self.fill(logs)
    for entry in task_entries(0, 1500):
      logs.write_task_line(entry)
    limit(total=(self.total(logs) + logs._store("tasks").live_bytes()) // 2)
    assert logs.trim_total() > 0
    assert config.LOG_KEEP_NEWEST_ROWS <= len(rows(logs, "tasks")) < 1500
    assert len(rows(logs, "new-app")) == 1500

  def test_the_total_is_checked_after_a_write(self, logs, small, limit, monkeypatch):
    self.fill(logs)
    limit(total=self.total(logs) // 2)
    monkeypatch.setattr(logs, "_total_checked", float("-inf"))
    logs.write_application_lines("new-app", lines(3500, 10, word="new"))
    assert self.total(logs) <= config.LOG_TOTAL_MAX_BYTES * 1.05


class TestCleanup:
  def test_lines_past_the_retention_and_the_shards_of_anything_but_an_application_go(self, logs, application):
    kept = application.qualified_name
    old = (utcnow() - timedelta(days=config.RETENTION_DAYS + 1)).isoformat()
    fresh = utcnow().isoformat()
    logs.write_application_lines(kept, [{"hostname": "w1", "ts": old, "stream": "stdout", "message": "stale"},
                                        {"hostname": "w1", "ts": fresh, "stream": "stdout", "message": "recent"}])
    for name in ("deleted-app", "sage", "traefik", "cloudflared", "telegraf"):
      logs.write_application_lines(name, lines(0, 5))
    logs.cleanup()
    assert [row["message"] for row in logs.query_application_lines(kept)["lines"]] == ["recent"]
    assert [path.stem for path in (logs.db_path / "logs").glob("*.db")] == [kept]

  def test_a_line_older_than_the_cutoff_goes_whatever_the_precision_of_its_time(self, logs, application, clock):
    clock.set(datetime(2026, 1, 15, 12, 0, 0, 500000))
    name = application.qualified_name
    logs.ingest("w1", push({**event(name, "half a second too old"), "timestamp": "2026-01-01T12:00:00Z"},
                           {**event(name, "just in time"), "timestamp": "2026-01-01T12:00:00.6Z"}))

    logs.cleanup(days=14)

    assert [row["message"] for row in logs.query_application_lines(name)["lines"]] == ["just in time"]

  def test_the_requests_are_kept_and_lose_their_rows_past_the_retention(self, logs, db):
    old = (utcnow() - timedelta(days=config.RETENTION_DAYS + 1)).isoformat()
    logs.ingest("w1", push(event("traefik", access_line(StartUTC=old, RequestPath="/stale")),
                           event("traefik", access_line(StartUTC=utcnow().isoformat(), RequestPath="/recent"))))
    logs.cleanup()
    assert [row.path for row in logs._store("requests").lines.select()] == ["/recent"]

  def test_the_task_logs_are_kept_and_lose_their_lines_past_the_retention(self, logs, db):
    old = (utcnow() - timedelta(days=config.RETENTION_DAYS + 1)).isoformat()
    logs.write_task_line({**task_entries(0, 1)[0], "ts": old, "message": "stale"})
    logs.write_task_line({**task_entries(1, 1)[0], "ts": utcnow().isoformat(), "message": "recent"})
    logs.cleanup()
    assert [line["message"] for line in logs.query_task_logs("t1")["lines"]] == ["recent"]

  def test_it_applies_the_size_limits(self, logs, application, small, limit):
    name = application.qualified_name
    logs.write_application_lines(name, lines(0, 3000))
    logs.ingest("w1", push(*(event("traefik", access_line(index)) for index in range(3000))))
    limit(shard=logs._store("requests").live_bytes() // 2)
    logs.cleanup(days=36500)
    assert 0 < len(rows(logs, name)) < 3000
    assert 0 < len(rows(logs, "requests")) < 3000


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
          logs.write_application_lines(container, next_lines(rng, container))
      except Exception:
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
      except Exception:
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
      assert shard.db.execute_sql("PRAGMA integrity_check").fetchone()[0] == "ok"
      if shard.index is not None:
        shard.db.execute_sql("INSERT INTO containerlogsindex(containerlogsindex, rank) VALUES('integrity-check', 1)")
      assert shard.live_bytes() <= config.LOG_SHARD_MAX_BYTES, name
      assert rows(logs, name)[-1] == newest[name], f"the newest line of {name} was trimmed"
      assert len(rows(logs, name)) >= config.LOG_KEEP_NEWEST_ROWS
    assert sum(logs._store(name).live_bytes() for name in containers + ["tasks"]) <= config.LOG_TOTAL_MAX_BYTES

  def test_a_trim_while_another_thread_writes_the_same_shard(self, logs, small, limit, monkeypatch):
    monkeypatch.setattr(config, "LOG_SIZE_CHECK_SECONDS", 10**9)
    monkeypatch.setattr(config, "LOG_TRIM_BATCH_ROWS", 10)
    logs.write_application_lines("web", lines(0, 400))
    errors = []

    def write():
      try:
        for index in range(400):
          logs.write_application_lines("web", lines(400 + index * 5, 5))
      except Exception:
        errors.append(traceback.format_exc())
      finally:
        close_connections(logs)

    writer = threading.Thread(target=write)
    writer.start()
    try:
      while writer.is_alive():
        limit(shard=logs.application_store("web").live_bytes() * 3 // 4)
        logs.trim_shard("web")
    finally:
      writer.join()
    assert errors == [], errors[0] if errors else ""
    assert logs.application_store("web").db.execute_sql("PRAGMA integrity_check").fetchone()[0] == "ok"
