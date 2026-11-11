from datetime import datetime, timedelta

from services.metrics.series import PERIODS, aggregate_buckets, fill_missing_values

START = datetime(2026, 10, 23, 10, 5)


def iso(minutes_before_start=0):
  return (START - timedelta(minutes=minutes_before_start)).isoformat() + "Z"


def test_each_period_asks_for_exactly_its_window():
  for period in PERIODS.values():
    assert period["bucket"] * period["points"] == period["delta"] / timedelta(minutes=1)


class TestFillMissingValues:
  def test_with_no_rows_every_minute_is_an_empty_row_ending_at_the_start(self):
    rows = fill_missing_values([], START, 3, {"cpu": None})
    assert rows == [{"ts": iso(2), "cpu": None}, {"ts": iso(1), "cpu": None}, {"ts": iso(0), "cpu": None}]

  def test_a_row_stays_in_its_minute_and_the_gaps_are_filled(self):
    rows = [{"ts": iso(2), "cpu": 1.0}, {"ts": iso(0), "cpu": 3.0}]

    filled = fill_missing_values(rows, START, 4, {"cpu": None})

    assert [(row["ts"], row["cpu"]) for row in filled] == [(iso(3), None), (iso(2), 1.0), (iso(1), None), (iso(0), 3.0)]

  def test_the_result_has_exactly_the_requested_number_of_rows(self):
    assert len(fill_missing_values([], START, 60, {"cpu": None})) == 60

  def test_rows_older_than_the_window_are_left_out(self):
    rows = [{"ts": iso(10), "cpu": 9.0}, {"ts": iso(1), "cpu": 2.0}]

    filled = fill_missing_values(rows, START, 3, {"cpu": None})

    assert [row["cpu"] for row in filled] == [None, 2.0, None]

  def test_every_template_field_is_in_an_empty_row(self):
    row = fill_missing_values([], START, 1, {"cpu": None, "mem": None, "name": "web"})[0]
    assert row == {"ts": iso(0), "cpu": None, "mem": None, "name": "web"}

  def test_rows_newer_than_the_start_are_ignored_and_the_older_ones_still_match(self):
    newer = [{"ts": (START + timedelta(minutes=minutes)).isoformat() + "Z", "cpu": 9.0} for minutes in (1, 2)]
    rows = [{"ts": iso(1), "cpu": 2.0}, {"ts": iso(0), "cpu": 3.0}, *newer]

    filled = fill_missing_values(rows, START, 3, {"cpu": None})

    assert [row["cpu"] for row in filled] == [None, 2.0, 3.0]

  def test_a_row_between_two_minutes_is_ignored_and_the_older_ones_still_match(self):
    between = {"ts": (START - timedelta(seconds=30)).isoformat() + "Z", "cpu": 5.0}
    rows = [{"ts": iso(2), "cpu": 1.0}, between, {"ts": iso(0), "cpu": 3.0}]

    filled = fill_missing_values(rows, START, 3, {"cpu": None})

    assert [row["cpu"] for row in filled] == [1.0, None, 3.0]


class TestAggregateBuckets:
  def test_no_rows_give_no_buckets(self):
    assert aggregate_buckets([], 2, ["ts"]) == []

  def test_a_bucket_of_one_keeps_the_row_rounded_to_two_places(self):
    assert aggregate_buckets([{"ts": "t1", "cpu": 1.234}], 1, ["ts"]) == [{"ts": "t1", "cpu": 1.23}]

  def test_a_bucket_is_the_average_of_its_rows_under_the_first_timestamp(self):
    rows = [{"ts": "t1", "cpu": 10}, {"ts": "t2", "cpu": 20}, {"ts": "t3", "cpu": 30}, {"ts": "t4", "cpu": 50}]
    assert aggregate_buckets(rows, 2, ["ts"]) == [{"ts": "t1", "cpu": 15.0}, {"ts": "t3", "cpu": 40.0}]

  def test_every_field_is_averaged_on_its_own(self):
    rows = [{"ts": "t1", "cpu": 10, "mem": 100}, {"ts": "t2", "cpu": 20, "mem": 300}]
    assert aggregate_buckets(rows, 2, ["ts"]) == [{"ts": "t1", "cpu": 15.0, "mem": 200.0}]

  def test_an_average_is_rounded_to_two_places(self):
    rows = [{"ts": "t1", "cpu": 1}, {"ts": "t2", "cpu": 2}, {"ts": "t3", "cpu": 2}]
    assert aggregate_buckets(rows, 3, ["ts"])[0]["cpu"] == 1.67

  def test_a_missing_value_is_left_out_of_the_average(self):
    rows = [{"ts": "t1", "cpu": 10}, {"ts": "t2", "cpu": None}, {"ts": "t3", "cpu": 20}]
    assert aggregate_buckets(rows, 3, ["ts"])[0]["cpu"] == 15.0

  def test_a_bucket_with_no_values_is_null(self):
    rows = [{"ts": "t1", "cpu": None}, {"ts": "t2", "cpu": None}]
    assert aggregate_buckets(rows, 2, ["ts"]) == [{"ts": "t1", "cpu": None}]

  def test_rows_that_do_not_fill_a_bucket_are_dropped(self):
    rows = [{"ts": f"t{i}", "cpu": i} for i in range(5)]
    assert [row["ts"] for row in aggregate_buckets(rows, 2, ["ts"])] == ["t0", "t2"]

  def test_fewer_rows_than_a_bucket_give_nothing(self):
    assert aggregate_buckets([{"ts": "t1", "cpu": 1}] * 3, 4, ["ts"]) == []

  def test_the_listed_fields_are_kept_from_the_first_row_and_not_averaged(self):
    rows = [{"ts": "t1", "name": "web", "cpu": 10}, {"ts": "t2", "name": "web", "cpu": 30}]
    assert aggregate_buckets(rows, 2, ["ts", "name"]) == [{"ts": "t1", "name": "web", "cpu": 20.0}]
