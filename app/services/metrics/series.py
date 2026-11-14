"""The graphs' series: the window and buckets of each period, from rows of one minute each."""
from datetime import timedelta

# delta is period start end range
# buckets is number of points to aggregate
# points is number of data points to return
# data in db is stored in minute intervals
PERIODS = {
    "1m": {"delta": timedelta(minutes=2), "bucket": 1, "points": 2},
    "1h": {"delta": timedelta(hours=1), "bucket": 1, "points": 60},
    "24h": {"delta": timedelta(hours=24), "bucket": 24, "points": 60},
    "1w": {"delta": timedelta(days=7), "bucket": 168, "points": 60},
}


def aggregate_buckets(rows, bucket, agg_fields):
  """
  Aggregate rows into buckets by averaging values within each bucket time range.
  Aggregate all fields except for `agg_fields` fields.
  Aggregate functions will be average. Missing values are left out of the average; a field with no values in a bucket is null.
  """
  keys = set(rows[0].keys()) - set(agg_fields) if rows else set()

  # Create the aggregate list
  agg_rows_length = len(rows) // bucket
  agg_rows = []

  for i in range(agg_rows_length):
    bucket_rows = rows[i * bucket: (i + 1) * bucket]
    agg_row = {k: bucket_rows[0][k] for k in agg_fields}

    for k in keys:
      values = [r[k] for r in bucket_rows if r[k] is not None]
      agg_row[k] = round(sum(values) / len(values), 2) if values else None
    agg_rows.append(agg_row)

  return agg_rows


def fill_missing_values(rows, start, target_points, template):
  """
  Fill rows with missing time periods (ts) in increments of 1 min.
  Creates target_points rows spanning the full period.
  """
  result = []
  current = start
  idx = len(rows) - 1  # Start from the newest row (end of list)
  while len(result) < target_points:
    current_iso = current.isoformat() + "Z"
    # A row past the start, or between two minutes, fits no slot and must not hold back the older ones.
    while idx >= 0 and rows[idx]["ts"] > current_iso:
      idx -= 1
    if idx >= 0 and rows[idx]["ts"] == current_iso:
      result.append(rows[idx])
    else:
      # Insert empty row for missing bucket
      result.append({"ts": current_iso, **template})
    current -= timedelta(minutes=1)

  return list(reversed(result))
