import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from services.metrics import _PERIODS, Metrics, MinutePeaks, WorkerPeaks
from services.worker_reports import ContainerReading, HostReading, Snapshot, parse_batch

START = datetime(2026, 10, 23, 10, 5)


def iso(minutes_before_start=0):
  return (START - timedelta(minutes=minutes_before_start)).isoformat() + "Z"


@pytest.fixture
def metrics():
  """Without __init__, which starts the background sampler thread."""
  return Metrics.__new__(Metrics)


def test_each_period_asks_for_exactly_its_window():
  for period in _PERIODS.values():
    assert period["bucket"] * period["points"] == period["delta"] / timedelta(minutes=1)


class TestFillMissingValues:
  def test_with_no_rows_every_minute_is_an_empty_row_ending_at_the_start(self, metrics):
    rows = metrics._fill_missing_values([], START, 3, {"cpu": None})
    assert rows == [{"ts": iso(2), "cpu": None}, {"ts": iso(1), "cpu": None}, {"ts": iso(0), "cpu": None}]

  def test_a_row_stays_in_its_minute_and_the_gaps_are_filled(self, metrics):
    rows = [{"ts": iso(2), "cpu": 1.0}, {"ts": iso(0), "cpu": 3.0}]

    filled = metrics._fill_missing_values(rows, START, 4, {"cpu": None})

    assert [(row["ts"], row["cpu"]) for row in filled] == [(iso(3), None), (iso(2), 1.0), (iso(1), None), (iso(0), 3.0)]

  def test_the_result_has_exactly_the_requested_number_of_rows(self, metrics):
    assert len(metrics._fill_missing_values([], START, 60, {"cpu": None})) == 60

  def test_rows_older_than_the_window_are_left_out(self, metrics):
    rows = [{"ts": iso(10), "cpu": 9.0}, {"ts": iso(1), "cpu": 2.0}]

    filled = metrics._fill_missing_values(rows, START, 3, {"cpu": None})

    assert [row["cpu"] for row in filled] == [None, 2.0, None]

  def test_every_template_field_is_in_an_empty_row(self, metrics):
    row = metrics._fill_missing_values([], START, 1, {"cpu": None, "mem": None, "name": "web"})[0]
    assert row == {"ts": iso(0), "cpu": None, "mem": None, "name": "web"}

  def test_rows_newer_than_the_start_are_ignored_and_the_older_ones_still_match(self, metrics):
    newer = [{"ts": (START + timedelta(minutes=minutes)).isoformat() + "Z", "cpu": 9.0} for minutes in (1, 2)]
    rows = [{"ts": iso(1), "cpu": 2.0}, {"ts": iso(0), "cpu": 3.0}, *newer]

    filled = metrics._fill_missing_values(rows, START, 3, {"cpu": None})

    assert [row["cpu"] for row in filled] == [None, 2.0, 3.0]

  def test_a_row_between_two_minutes_is_ignored_and_the_older_ones_still_match(self, metrics):
    between = {"ts": (START - timedelta(seconds=30)).isoformat() + "Z", "cpu": 5.0}
    rows = [{"ts": iso(2), "cpu": 1.0}, between, {"ts": iso(0), "cpu": 3.0}]

    filled = metrics._fill_missing_values(rows, START, 3, {"cpu": None})

    assert [row["cpu"] for row in filled] == [1.0, None, 3.0]


class TestAggregateBuckets:
  def test_no_rows_give_no_buckets(self, metrics):
    assert metrics._aggregate_buckets([], 2, ["ts"]) == []

  def test_a_bucket_of_one_keeps_the_row_rounded_to_two_places(self, metrics):
    assert metrics._aggregate_buckets([{"ts": "t1", "cpu": 1.234}], 1, ["ts"]) == [{"ts": "t1", "cpu": 1.23}]

  def test_a_bucket_is_the_average_of_its_rows_under_the_first_timestamp(self, metrics):
    rows = [{"ts": "t1", "cpu": 10}, {"ts": "t2", "cpu": 20}, {"ts": "t3", "cpu": 30}, {"ts": "t4", "cpu": 50}]
    assert metrics._aggregate_buckets(rows, 2, ["ts"]) == [{"ts": "t1", "cpu": 15.0}, {"ts": "t3", "cpu": 40.0}]

  def test_every_field_is_averaged_on_its_own(self, metrics):
    rows = [{"ts": "t1", "cpu": 10, "mem": 100}, {"ts": "t2", "cpu": 20, "mem": 300}]
    assert metrics._aggregate_buckets(rows, 2, ["ts"]) == [{"ts": "t1", "cpu": 15.0, "mem": 200.0}]

  def test_an_average_is_rounded_to_two_places(self, metrics):
    rows = [{"ts": "t1", "cpu": 1}, {"ts": "t2", "cpu": 2}, {"ts": "t3", "cpu": 2}]
    assert metrics._aggregate_buckets(rows, 3, ["ts"])[0]["cpu"] == 1.67

  def test_a_missing_value_is_left_out_of_the_average(self, metrics):
    rows = [{"ts": "t1", "cpu": 10}, {"ts": "t2", "cpu": None}, {"ts": "t3", "cpu": 20}]
    assert metrics._aggregate_buckets(rows, 3, ["ts"])[0]["cpu"] == 15.0

  def test_a_bucket_with_no_values_is_null(self, metrics):
    rows = [{"ts": "t1", "cpu": None}, {"ts": "t2", "cpu": None}]
    assert metrics._aggregate_buckets(rows, 2, ["ts"]) == [{"ts": "t1", "cpu": None}]

  def test_rows_that_do_not_fill_a_bucket_are_dropped(self, metrics):
    rows = [{"ts": f"t{i}", "cpu": i} for i in range(5)]
    assert [row["ts"] for row in metrics._aggregate_buckets(rows, 2, ["ts"])] == ["t0", "t2"]

  def test_fewer_rows_than_a_bucket_give_nothing(self, metrics):
    assert metrics._aggregate_buckets([{"ts": "t1", "cpu": 1}] * 3, 4, ["ts"]) == []

  def test_the_listed_fields_are_kept_from_the_first_row_and_not_averaged(self, metrics):
    rows = [{"ts": "t1", "name": "web", "cpu": 10}, {"ts": "t2", "name": "web", "cpu": 30}]
    assert metrics._aggregate_buckets(rows, 2, ["ts", "name"]) == [{"ts": "t1", "name": "web", "cpu": 20.0}]


@pytest.fixture
def store(data_dir):
  """A Metrics with its shards in a temporary directory and no sampler thread."""
  (data_dir / "metrics").mkdir()
  metrics = Metrics.__new__(Metrics)
  metrics.lock = threading.RLock()
  metrics._dbs = {"metrics": {}}
  metrics._workers = {}
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


MINUTE = datetime(2026, 10, 23, 10, 5)


def host(idle=90.0, rx=None, tx=None, **overrides):
  values = dict(cpu_idle_percent=idle, cores=4, mem_total=8 * 1024**3, mem_used=1024**3, mem_cached=512 * 1024**2,
                mem_available=None, load1=0.5, load5=0.4, load15=0.3, disk_total=100 * 1024**3, disk_used=10 * 1024**3,
                net_rx_bytes=rx, net_tx_bytes=tx)
  return HostReading(**{**values, **overrides})


def container(name="web", status="running", cpu=100.0, mem=100 * 1024**2, rx=None, tx=None, started_at=1):
  return ContainerReading(name=name, status=status, image="img", deployed_at=None, exit_code=0, oom_killed=False,
                          started_at=started_at, finished_at=None, health=None, cpu_percent=cpu, mem_bytes=mem,
                          net_rx_bytes=rx, net_tx_bytes=tx)


def snapshot(seconds, host_reading=None, *containers):
  return Snapshot(at=MINUTE + timedelta(seconds=seconds), host=host_reading or host(),
                  containers={c.name: c for c in containers}, traefik_reachable=True)


class TestMinutePeaks:
  def test_keeps_the_highest_reading_of_each_kind_in_a_minute(self):
    peaks = MinutePeaks()
    peaks.fold(MINUTE, {"cpu": 5, "mem": 100})
    peaks.fold(MINUTE, {"cpu": 9, "mem": 80})
    peaks.fold(MINUTE, {"cpu": 7})
    assert peaks.drain(MINUTE + timedelta(minutes=1)) == {MINUTE: {"cpu": 9, "mem": 100}}

  def test_a_reading_that_is_not_a_peak_keeps_its_latest_value(self):
    peaks = MinutePeaks()
    peaks.fold(MINUTE, {"cpu": 5}, {"cores": 2})
    peaks.fold(MINUTE, {"cpu": 1}, {"cores": 4})
    assert peaks.drain(MINUTE + timedelta(minutes=1)) == {MINUTE: {"cpu": 5, "cores": 4}}

  def test_drain_returns_only_the_minutes_before_the_given_one_and_removes_them(self):
    peaks = MinutePeaks()
    for minutes in range(3):
      peaks.fold(MINUTE + timedelta(minutes=minutes), {"cpu": minutes})
    assert peaks.drain(MINUTE + timedelta(minutes=2)) == {MINUTE: {"cpu": 0}, MINUTE + timedelta(minutes=1): {"cpu": 1}}
    assert len(peaks) == 1

  def test_minutes_that_were_never_drained_are_dropped_after_the_keep_window(self):
    peaks = MinutePeaks(keep=timedelta(minutes=10))
    peaks.fold(MINUTE, {"cpu": 1})
    peaks.fold(MINUTE + timedelta(minutes=11), {"cpu": 2})
    assert peaks.drain(MINUTE + timedelta(minutes=12)) == {MINUTE + timedelta(minutes=11): {"cpu": 2}}


class TestWorkerPeaks:
  def host_minute(self, peaks):
    return peaks.drain(MINUTE + timedelta(minutes=1))[0][MINUTE]

  def test_the_cpu_is_what_the_host_was_not_idle(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(idle=90)))
    peaks.fold(snapshot(10, host(idle=40)))
    peaks.fold(snapshot(20, host(idle=70)))
    assert self.host_minute(peaks)["cpu_pct"] == 60

  def test_memory_and_disk_are_stored_in_the_units_of_the_rows(self):
    minute = (lambda p: (p.fold(snapshot(0)), self.host_minute(p))[1])(WorkerPeaks())
    assert (minute["mem_used_mb"], minute["mem_cached_mb"], minute["disk_used_gb"]) == (1024, 512, 10.0)
    assert (minute["cpu_cores"], minute["mem_total_mb"], minute["disk_total_gb"]) == (4, 8192, 100.0)

  def test_the_network_rate_is_the_counter_change_over_the_time_between_reports(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=0, tx=0)))
    peaks.fold(snapshot(10, host(rx=102_400, tx=10_240)))
    minute = self.host_minute(peaks)
    assert (minute["net_rx_kbps"], minute["net_tx_kbps"]) == (10.0, 1.0)

  def test_an_idle_interface_is_a_rate_of_zero_not_a_missing_one(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=500, tx=500)))
    peaks.fold(snapshot(10, host(rx=500, tx=500)))
    minute = self.host_minute(peaks)
    assert (minute["net_rx_kbps"], minute["net_tx_kbps"]) == (0.0, 0.0)

  def test_the_first_report_has_no_network_rate(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=5_000_000, tx=5_000_000)))
    assert "net_rx_kbps" not in self.host_minute(peaks)

  def test_a_counter_that_went_down_gives_no_rate_for_that_report(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=1_000_000, tx=0)))
    peaks.fold(snapshot(10, host(rx=100, tx=0)))
    assert "net_rx_kbps" not in self.host_minute(peaks)

  def test_after_a_long_silence_a_counters_change_is_not_a_rate(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=0, tx=0)))
    peaks.fold(snapshot(45, host(rx=10_000_000, tx=0)))
    assert "net_rx_kbps" not in self.host_minute(peaks)

  def test_a_repeated_or_older_snapshot_is_ignored(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(20, host(idle=50)))
    peaks.fold(snapshot(20, host(idle=0)))
    peaks.fold(snapshot(10, host(idle=0)))
    assert self.host_minute(peaks)["cpu_pct"] == 50

  def test_a_reading_the_report_lacks_is_left_out(self):
    minute = (lambda p: (p.fold(snapshot(0, host(idle=None, mem_used=None))), self.host_minute(p))[1])(WorkerPeaks())
    assert "cpu_pct" not in minute and "mem_used_mb" not in minute and "mem_cached_mb" in minute

  def test_a_running_container_shares_dockers_percentage_over_the_cores(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(cores=4), container(cpu=150.0, mem=300 * 1024**2)))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert containers["web"][MINUTE] == {"cpu_pct": 37.5, "mem_used_mb": 300}

  def test_a_container_that_is_not_running_is_left_out(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container(status="exited", cpu=0, mem=0), container("db")))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert set(containers) == {"db"}

  def test_a_containers_network_rate_comes_from_its_own_counters(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container(rx=1_000, tx=0)))
    peaks.fold(snapshot(10, host(), container(rx=1_000 + 20_480, tx=10_240)))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert (containers["web"][MINUTE]["net_rx_kbps"], containers["web"][MINUTE]["net_tx_kbps"]) == (2.0, 1.0)

  def test_a_new_run_of_a_container_has_no_rate_even_if_its_counter_is_higher(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container(rx=1_000, started_at=1)))
    peaks.fold(snapshot(10, host(), container(rx=900_000, started_at=2)))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert "net_rx_kbps" not in containers["web"][MINUTE]

  def test_a_container_with_nothing_pending_is_forgotten(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container()))
    peaks.drain(MINUTE + timedelta(minutes=1))
    assert peaks.containers == {}


BATCH = json.loads((Path(__file__).parent.parent / "data" / "telegraf_batch.json").read_text())


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

  def test_flush_workers_returns_the_newest_minute_of_each_worker_that_had_one(self, store, clock):
    store.ingest("w1", [snapshot(0, host(idle=80))])
    store.ingest("w2", [snapshot(0, host(idle=None))])
    clock.set(MINUTE + timedelta(minutes=1))
    newest = store.flush_workers()
    assert list(newest) == ["w1"] and newest["w1"]["cpu_pct"] == 20.0

  def test_flush_workers_writes_every_worker_that_reported(self, store, clock):
    store.ingest("w1", [snapshot(0, host(idle=80))])
    store.ingest("w2", [snapshot(0, host(idle=60))])
    clock.set(MINUTE + timedelta(minutes=1))
    store.flush_workers()
    cpu = {name: [row["cpu_pct"] for row in store.get_metrics_db(name)["models"]["WorkerMetrics"].select().dicts()]
           for name in ("w1", "w2")}
    assert cpu == {"w1": [20.0], "w2": [40.0]}

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


class TestRows:
  def test_a_minute_lacking_a_core_reading_has_no_row(self):
    from services.metrics import _host_row
    assert _host_row(MINUTE, {"cpu_pct": 1, "mem_used_mb": 1, "mem_cached_mb": 1}) is None

  def test_the_network_rates_are_zero_and_the_load_is_none_when_not_reported(self):
    from services.metrics import _host_row
    row = _host_row(MINUTE, {"cpu_pct": 1, "mem_used_mb": 2, "mem_cached_mb": 3, "disk_used_gb": 4})
    assert (row["net_rx_kbps"], row["net_tx_kbps"], row["load_avg_1m"]) == (0.0, 0.0, None)
