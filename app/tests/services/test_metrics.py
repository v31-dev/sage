import threading
from datetime import datetime, timedelta

import pytest

from services.metrics import _PERIODS, Metrics

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


class TestQueryPeriodClock:
  """The buckets are UTC minutes, which the `Z` on every timestamp says, on any machine."""

  @pytest.fixture
  def store(self, data_dir):
    (data_dir / "metrics").mkdir()
    metrics = Metrics.__new__(Metrics)
    metrics.lock = threading.RLock()
    metrics._dbs = {"metrics": {}}
    metrics.db_path = data_dir
    return metrics

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
