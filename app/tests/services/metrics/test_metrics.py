import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from services.metrics import Metrics
from services.metrics.peaks import MinutePeaks
from services.updates import UpdateSource
from services.worker_reports import parse_batch
from tests.services.metrics.test_peaks import MINUTE, container, host, snapshot


@pytest.fixture
def store(data_dir):
  """A Metrics with its shards in a temporary directory and no sampler thread."""
  (data_dir / "metrics").mkdir()
  metrics = Metrics.__new__(Metrics)
  metrics.lock = threading.RLock()
  metrics._dbs = {"metrics": {}}
  metrics._workers = {}
  metrics._newest = {}
  metrics._peaks = MinutePeaks()
  metrics.db_path = data_dir
  yield metrics
  for shard in metrics._dbs["metrics"].values():
    shard["db"].close()


class TestQueryPeriodClock:
  """The buckets are UTC minutes, which the `Z` on every timestamp says, on any machine."""

  def write_minute(self, store, ts):
    WorkerMetrics = store.get_metrics_db("w1")["models"]["WorkerMetrics"]
    WorkerMetrics.create(ts=ts, cpu_pct=50.0, mem_used_mb=100, mem_cached_mb=10, disk_used_gb=1.0, net_rx_kbps=1.0, net_tx_kbps=2.0)

  @pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata", "America/Los_Angeles"])
  def test_the_window_ends_at_the_current_utc_minute(self, store, clock, machine_timezone, zone):
    machine_timezone(zone)
    clock.set(datetime(2026, 10, 23, 10, 5, 30))
    self.write_minute(store, datetime(2026, 10, 23, 10, 4))

    rows = store.query_period("w1", "1h")["host"]

    assert len(rows) == 60
    assert rows[-1]["ts"] == "2026-10-23T10:05:00Z"
    assert [row["ts"] for row in rows if row["cpu_pct"] is not None] == ["2026-10-23T10:04:00Z"]

  @pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata", "America/Los_Angeles"])
  def test_a_containers_window_ends_at_the_current_utc_minute_too(self, store, clock, machine_timezone, zone):
    machine_timezone(zone)
    clock.set(datetime(2026, 10, 23, 10, 5, 30))
    ContainerMetrics = store.get_metrics_db("w1")["models"]["ContainerMetrics"]
    ContainerMetrics.create(name="demo-web", ts=datetime(2026, 10, 23, 10, 3), cpu_pct=5.0, mem_used_mb=50.0, net_rx_kbps=1.0, net_tx_kbps=1.0)

    rows = store.query_container_period("w1", "demo-web", "1h")

    assert len(rows) == 60
    assert rows[-1]["ts"] == "2026-10-23T10:05:00Z"
    assert [row["ts"] for row in rows if row["cpu_pct"] is not None] == ["2026-10-23T10:03:00Z"]


BATCH = json.loads((Path(__file__).parents[2] / "data" / "telegraf_batch.json").read_text())


class TestFlushWorker:
  def rows(self, store, model):
    return list(store.get_metrics_db("w1")["models"][model].select().dicts())

  def ingest_the_recorded_batch(self, store, clock):
    snapshots = parse_batch(BATCH)
    store.ingest("w1", snapshots)
    clock.set(snapshots[0].at.replace(second=0) + timedelta(minutes=1, seconds=5))
    return snapshots

  def test_the_completed_minute_is_written_as_one_row_of_peaks(self, store, clock):
    snapshots = self.ingest_the_recorded_batch(store, clock)
    store.flush_worker("w1")
    (row,) = self.rows(store, "WorkerMetrics")
    assert row["ts"] == snapshots[0].at.replace(second=0)
    assert row["cpu_pct"] == pytest.approx(max(100 - s.host.cpu_idle_percent for s in snapshots), abs=0.01)
    assert row["mem_used_mb"] == max(s.host.mem_used for s in snapshots) // 1024**2
    assert row["net_rx_kbps"] > 0

  def test_the_load_averages_are_written_with_the_minute(self, store, clock):
    snapshots = self.ingest_the_recorded_batch(store, clock)
    store.flush_worker("w1")
    (row,) = self.rows(store, "WorkerMetrics")
    assert row["load_avg_1m"] == max(s.host.load1 for s in snapshots)
    assert row["load_avg_5m"] == max(s.host.load5 for s in snapshots)
    assert row["load_avg_15m"] == max(s.host.load15 for s in snapshots)

  def test_the_workers_size_is_written_once(self, store, clock):
    self.ingest_the_recorded_batch(store, clock)
    store.flush_worker("w1")
    (meta,) = self.rows(store, "WorkerMeta")
    assert (meta["cpu_cores"], meta["mem_total_mb"]) == (16, 30769758208 // 1024**2)

  def test_each_running_container_gets_a_row_and_a_stopped_one_does_not(self, store, clock):
    self.ingest_the_recorded_batch(store, clock)
    store.flush_worker("w1")
    assert sorted(row["name"] for row in self.rows(store, "ContainerMetrics")) == ["traefik", "web"]

  def test_each_minute_of_a_container_is_a_row_of_its_own(self, store, clock):
    store.ingest("w1", [snapshot(0, host(), container(cpu=40.0)), snapshot(60, host(), container(cpu=80.0))])
    clock.set(MINUTE + timedelta(minutes=2))
    store.flush_worker("w1")
    assert [(row["ts"], row["cpu_pct"]) for row in self.rows(store, "ContainerMetrics")] == [
        (MINUTE, 10.0), (MINUTE + timedelta(minutes=1), 20.0)]

  def test_the_minute_in_progress_is_left_to_keep_accumulating(self, store, clock):
    snapshots = self.ingest_the_recorded_batch(store, clock)
    clock.set(snapshots[0].at.replace(second=0) + timedelta(seconds=30))
    store.flush_worker("w1")
    assert self.rows(store, "WorkerMetrics") == []
    clock.advance(timedelta(minutes=1))
    store.flush_worker("w1")
    assert len(self.rows(store, "WorkerMetrics")) == 1

  def test_a_minute_is_written_once_its_flush_has_run(self, store, clock):
    self.ingest_the_recorded_batch(store, clock)
    store.flush_worker("w1")
    store.flush_worker("w1")
    assert len(self.rows(store, "WorkerMetrics")) == 1

  def test_a_minute_missing_a_core_reading_is_not_written(self, store, clock):
    store.ingest("w1", [snapshot(0, host(idle=None))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush_worker("w1")
    assert self.rows(store, "WorkerMetrics") == []

  def test_a_minute_without_a_network_rate_stores_zero(self, store, clock):
    store.ingest("w1", [snapshot(0, host(rx=5, tx=5))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush_worker("w1")
    (row,) = self.rows(store, "WorkerMetrics")
    assert (row["net_rx_kbps"], row["net_tx_kbps"]) == (0.0, 0.0)

  def test_a_worker_that_never_reported_has_nothing_to_write(self, store, clock):
    assert store.flush_worker("w1") is None
    assert store._dbs["metrics"] == {}

  def test_it_returns_the_newest_row_it_wrote_with_the_machines_size(self, store, clock):
    self.ingest_the_recorded_batch(store, clock)
    minute = store.flush_worker("w1")
    (row,) = self.rows(store, "WorkerMetrics")
    (meta,) = self.rows(store, "WorkerMeta")
    assert minute == {**{key: value for key, value in row.items() if key != "id"},
                      "cpu_cores": meta["cpu_cores"], "mem_total_mb": meta["mem_total_mb"],
                      "disk_total_gb": meta["disk_total_gb"]}

  def test_it_returns_the_newest_of_the_minutes_it_wrote(self, store, clock):
    store.ingest("w1", [snapshot(0, host(idle=80)), snapshot(60, host(idle=50))])
    clock.set(MINUTE + timedelta(minutes=2))
    minute = store.flush_worker("w1")
    assert len(self.rows(store, "WorkerMetrics")) == 2
    assert (minute["ts"], minute["cpu_pct"]) == (MINUTE + timedelta(minutes=1), 50.0)

  def test_it_returns_nothing_while_the_minute_is_in_progress(self, store, clock):
    snapshots = self.ingest_the_recorded_batch(store, clock)
    clock.set(snapshots[0].at.replace(second=0) + timedelta(seconds=30))
    assert store.flush_worker("w1") is None

  def test_a_worker_that_went_quiet_is_dropped_once_written(self, store, clock):
    store.ingest("w1", [snapshot(0, host(), container())])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush_worker("w1")
    assert "w1" not in store._workers

  def test_a_worker_with_a_report_in_the_current_minute_is_kept(self, store, clock):
    store.ingest("w1", [snapshot(0), snapshot(70)])
    clock.set(MINUTE + timedelta(seconds=75))
    store.flush_worker("w1")
    assert "w1" in store._workers
    assert len(self.rows(store, "WorkerMetrics")) == 1

  def test_forget_drops_what_was_not_flushed(self, store, clock):
    self.ingest_the_recorded_batch(store, clock)
    store.forget("w1")
    store.flush_worker("w1")
    assert store._dbs["metrics"] == {}

  def test_the_rows_are_what_the_ui_queries_read(self, store, clock):
    snapshots = self.ingest_the_recorded_batch(store, clock)
    store.flush_worker("w1")
    clock.set(snapshots[0].at.replace(second=0) + timedelta(minutes=1))
    result = store.query_period("w1", "1h")
    assert [row["cpu_pct"] for row in result["host"] if row["cpu_pct"] is not None] != []
    assert {c[0]["name"] for c in result["containers"]} == {"traefik", "web"}


SIZE = {"cpu_cores": 4, "mem_total_mb": 8192, "disk_total_gb": 100.0}


class TestFlush:
  def host_cpu(self, store, hostname):
    return [row["cpu_pct"] for row in store.get_metrics_db(hostname)["models"]["WorkerMetrics"].select().dicts()]

  def test_the_managers_and_every_workers_completed_minutes_are_written(self, store, clock, signals):
    store._peaks.fold(MINUTE, {"cpu_pct": 12.0, "mem_used_mb": 100, "mem_cached_mb": 10, "disk_used_gb": 1.0}, SIZE)
    store.ingest("w1", [snapshot(0, host(idle=80))])
    store.ingest("w2", [snapshot(0, host(idle=60))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush()
    assert {name: self.host_cpu(store, name) for name in ("manager", "w1", "w2")} == {
        "manager": [12.0], "w1": [20.0], "w2": [40.0]}

  def test_each_workers_newest_minute_is_kept_with_its_size_and_the_worker_list_is_told(self, store, clock, signals):
    store.ingest("w1", [snapshot(0, host(idle=80))])
    store.ingest("w2", [snapshot(0, host(idle=None))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush()
    newest = store.newest("w1")
    assert (newest["ts"], newest["cpu_pct"], {key: newest[key] for key in SIZE}) == (MINUTE, 20.0, SIZE)
    assert store.newest("w2") is None
    assert signals == [(UpdateSource.WORKER_LIST, None), (UpdateSource.WORKER_ENTRY, "w1")]

  def test_a_flush_that_writes_none_for_a_worker_drops_its_newest_minute_and_says_so(self, store, clock, signals):
    store.ingest("w1", [snapshot(0, host(idle=80))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush()
    signals.clear()
    clock.advance(timedelta(minutes=1))
    store.flush()
    assert store.newest("w1") is None
    assert signals == [(UpdateSource.WORKER_LIST, None), (UpdateSource.WORKER_ENTRY, "w1")]

  def test_a_flush_that_changes_no_newest_minute_says_nothing(self, store, clock, signals):
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush()
    store.ingest("w1", [snapshot(70, host(idle=80))])
    store.flush()
    assert (store.newest("w1"), signals) == (None, [])

  def test_a_new_minute_is_a_change(self, store, clock, signals):
    store.ingest("w1", [snapshot(0, host(idle=80)), snapshot(60, host(idle=80))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush()
    clock.advance(timedelta(minutes=1))
    store.flush()
    assert store.newest("w1")["ts"] == MINUTE + timedelta(minutes=1)
    assert signals.count((UpdateSource.WORKER_ENTRY, "w1")) == 2

  def test_a_removed_worker_has_no_newest_minute(self, store, clock, signals):
    store.ingest("w1", [snapshot(0, host(idle=80))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush()
    store.forget("w1")
    assert store.newest("w1") is None


class TestQueriesSeeTheMinutesNotYetWritten:
  """A minute is written to the shard once it has ended; the charts and the list cards show the latest
  reading, so the queries add what is still in memory."""

  def hour(self, store, hostname="w1"):
    return store.query_period(hostname, "1h")

  def host_ts_with_data(self, result):
    return [row["ts"][11:16] for row in result["host"] if row["cpu_pct"] is not None]

  def test_the_minute_in_progress_is_shown_before_it_is_written(self, store, clock):
    store.ingest("w1", [snapshot(10, host(idle=80))])
    clock.set(MINUTE + timedelta(seconds=30))
    result = self.hour(store)
    assert result["host"][-1]["ts"] == "2026-10-23T10:05:00Z"
    assert result["host"][-1]["cpu_pct"] == 20.0
    assert store.get_metrics_db("w1")["models"]["WorkerMetrics"].select().count() == 0

  def test_a_completed_minute_that_is_not_written_yet_is_shown_too(self, store, clock):
    store.ingest("w1", [snapshot(-50, host(idle=70)), snapshot(10, host(idle=80))])
    clock.set(MINUTE + timedelta(seconds=30))
    assert self.host_ts_with_data(self.hour(store)) == ["10:04", "10:05"]

  def test_once_written_a_minute_comes_from_the_shard_and_is_not_shown_twice(self, store, clock):
    store.ingest("w1", [snapshot(-50, host(idle=70)), snapshot(10, host(idle=80))])
    clock.set(MINUTE + timedelta(seconds=30))
    store.flush_worker("w1")
    result = self.hour(store)
    assert len(result["host"]) == 60
    assert self.host_ts_with_data(result) == ["10:04", "10:05"]
    assert [row["cpu_pct"] for row in result["host"] if row["cpu_pct"] is not None] == [30.0, 20.0]

  def test_a_minute_that_is_written_and_still_filling_shows_the_memory_not_the_old_row(self, store, clock):
    WorkerMetrics = store.get_metrics_db("w1")["models"]["WorkerMetrics"]
    WorkerMetrics.create(ts=MINUTE, cpu_pct=1.0, mem_used_mb=1, mem_cached_mb=1, disk_used_gb=1.0, net_rx_kbps=0, net_tx_kbps=0)
    store.ingest("w1", [snapshot(10, host(idle=50))])
    clock.set(MINUTE + timedelta(seconds=30))
    assert self.hour(store)["host"][-1]["cpu_pct"] == 50.0

  def test_the_latest_view_has_the_minute_before_in_the_first_seconds_of_a_minute(self, store, clock):
    store.ingest("w1", [snapshot(-10, host(idle=80))])
    clock.set(MINUTE + timedelta(seconds=3))
    rows = store.query_period("w1", "1m")["host"]
    assert [(row["ts"][11:16], row["cpu_pct"]) for row in rows] == [("10:04", 20.0), ("10:05", None)]

  def test_a_pending_minute_older_than_the_window_is_left_out(self, store, clock):
    store.ingest("w1", [snapshot(-240, host(idle=80))])
    clock.set(MINUTE + timedelta(seconds=30))
    assert self.host_ts_with_data(store.query_period("w1", "1m")) == []

  def test_a_minute_without_a_cpu_reading_is_not_shown(self, store, clock):
    store.ingest("w1", [snapshot(10, host(idle=None))])
    clock.set(MINUTE + timedelta(seconds=30))
    assert self.host_ts_with_data(self.hour(store)) == []

  def test_the_containers_in_memory_are_listed_with_their_latest_reading(self, store, clock):
    store.ingest("w1", [snapshot(10, host(), container("web", cpu=100.0), container("db", cpu=40.0))])
    clock.set(MINUTE + timedelta(seconds=30))
    series = self.hour(store)["containers"]
    assert [points[-1]["name"] for points in series] == ["db", "web"]
    assert [points[-1]["cpu_pct"] for points in series] == [10.0, 25.0]
    assert store.query_container_period("w1", "web", "1h")[-1]["cpu_pct"] == 25.0

  def test_a_container_written_and_one_in_memory_are_both_listed(self, store, clock):
    store.ingest("w1", [snapshot(-50, host(), container("old"))])
    clock.set(MINUTE + timedelta(seconds=30))
    store.flush_worker("w1")
    store.ingest("w1", [snapshot(40, host(), container("new"))])
    assert [points[-1]["name"] for points in self.hour(store)["containers"]] == ["new", "old"]

  def test_the_managers_own_minutes_in_memory_are_shown_too(self, store, clock):
    store._peaks.fold(MINUTE, {"cpu_pct": 12.0, "mem_used_mb": 100, "mem_cached_mb": 10, "disk_used_gb": 1.0,
                               "net_rx_kbps": 1.0, "net_tx_kbps": 2.0})
    clock.set(MINUTE + timedelta(seconds=30))
    last = store.query_period("manager", "1h")["host"][-1]
    assert (last["ts"], last["cpu_pct"], last["load_avg_1m"]) == ("2026-10-23T10:05:00Z", 12.0, None)

  def test_a_host_nothing_has_been_reported_for_has_no_rows(self, store, clock):
    clock.set(MINUTE + timedelta(seconds=30))
    assert self.host_ts_with_data(self.hour(store, "nobody")) == []
