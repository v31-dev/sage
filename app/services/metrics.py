import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from peewee import CharField, CompositeKey, DateTimeField, FloatField, IntegerField, Model, SqliteDatabase

import config
from services.base import Base
from services.db import Worker
from services.worker_reports import Snapshot
from utils.common import get_env, utcnow

logger = logging.getLogger(__name__)

# delta is period start end range
# buckets is number of points to aggregate
# points is number of data points to return
# data in db is stored in minute intervals
_PERIODS = {
    "1m": {"delta": timedelta(minutes=2), "bucket": 1, "points": 2},
    "1h": {"delta": timedelta(hours=1), "bucket": 1, "points": 60},
    "24h": {"delta": timedelta(hours=24), "bucket": 24, "points": 60},
    "1w": {"delta": timedelta(days=7), "bucket": 168, "points": 60},
}


_MIB = 1024**2
_GIB = 1024**3

# A minute of a worker's rows needs these; the network rates are 0 until there are two reports.
_REQUIRED_HOST_PEAKS = ("cpu_pct", "mem_used_mb", "mem_cached_mb", "disk_used_gb")


class MinutePeaks:
  """The highest reading of each kind in each minute, in memory until the minute has completed.
  A reading that is not a peak (a total, a core count) keeps its latest value. Not thread-safe:
  the caller holds a lock."""

  def __init__(self, keep: timedelta = timedelta(minutes=10)):
    self._keep = keep
    self._minutes: dict[datetime, dict] = {}

  def __len__(self):
    return len(self._minutes)

  def fold(self, minute: datetime, peaks: dict, latest: dict | None = None):
    # Completed minutes are drained every minute, so this normally holds one or two;
    # bound it in case the flush ever stalls.
    for stale in [m for m in self._minutes if m < minute - self._keep]:
      del self._minutes[stale]
    entry = self._minutes.setdefault(minute, {})
    for key, value in peaks.items():
      entry[key] = max(entry.get(key, value), value)
    entry.update(latest or {})

  def snapshot(self) -> dict[datetime, dict]:
    return {minute: dict(entry) for minute, entry in self._minutes.items()}

  def drain(self, before: datetime) -> dict[datetime, dict]:
    """Remove and return the minutes earlier than `before`."""
    completed = {minute: entry for minute, entry in self._minutes.items() if minute < before}
    for minute in completed:
      del self._minutes[minute]
    return completed


def _host_row(minute: datetime, peak: dict) -> dict | None:
  """A host's row for a minute from its peaks; None while the minute lacks a core reading. The network
  rates are 0 until there are two reports."""
  if not all(key in peak for key in _REQUIRED_HOST_PEAKS):
    return None
  return {
      "ts": minute,
      "cpu_pct": peak["cpu_pct"],
      "mem_used_mb": peak["mem_used_mb"],
      "mem_cached_mb": peak["mem_cached_mb"],
      "disk_used_gb": peak["disk_used_gb"],
      "load_avg_1m": peak.get("load_avg_1m"),
      "load_avg_5m": peak.get("load_avg_5m"),
      "load_avg_15m": peak.get("load_avg_15m"),
      "net_rx_kbps": peak.get("net_rx_kbps", 0.0),
      "net_tx_kbps": peak.get("net_tx_kbps", 0.0),
  }


def _container_row(name: str, minute: datetime, peak: dict) -> dict:
  return {
      "ts": minute,
      "name": name,
      "cpu_pct": peak.get("cpu_pct", 0.0),
      "mem_used_mb": peak.get("mem_used_mb", 0.0),
      "net_rx_kbps": peak.get("net_rx_kbps", 0.0),
      "net_tx_kbps": peak.get("net_tx_kbps", 0.0),
  }


def _present(**values):
  return {key: value for key, value in values.items() if value is not None}


def _rate_kbps(now, before, seconds):
  """Kilobytes a second between two readings of a byte counter; None when there is no earlier
  reading or the counter went down (a restart)."""
  if now is None or before is None or now < before:
    return None
  return round((now - before) / 1024 / seconds, 2)


class WorkerPeaks:
  """What one worker's reports add up to: the per-minute peaks of the host and of each running
  container. Not thread-safe: the caller holds a lock."""

  def __init__(self):
    self.host = MinutePeaks()
    self.containers: dict[str, MinutePeaks] = {}
    self._previous: Snapshot | None = None

  def fold(self, snapshot: Snapshot):
    previous = self._previous
    if previous is not None and snapshot.at <= previous.at:
      return  # a repeat, or a replay of an older gather
    self._previous = snapshot

    seconds = (snapshot.at - previous.at).total_seconds() if previous else 0
    if seconds > config.REPORT_INTERVAL_SECONDS * 3:
      previous = None  # after a long silence a counter's change is not a rate
    minute = snapshot.at.replace(second=0, microsecond=0)
    host = snapshot.host

    rx = _rate_kbps(host.net_rx_bytes, previous and previous.host.net_rx_bytes, seconds)
    tx = _rate_kbps(host.net_tx_bytes, previous and previous.host.net_tx_bytes, seconds)
    self.host.fold(
        minute,
        _present(
            cpu_pct=None if host.cpu_idle_percent is None else round(max(100 - host.cpu_idle_percent, 0), 2),
            mem_used_mb=None if host.mem_used is None else host.mem_used // _MIB,
            mem_cached_mb=None if host.mem_cached is None else host.mem_cached // _MIB,
            disk_used_gb=None if host.disk_used is None else round(host.disk_used / _GIB, 2),
            load_avg_1m=host.load1,
            load_avg_5m=host.load5,
            load_avg_15m=host.load15,
            net_rx_kbps=rx,
            net_tx_kbps=tx,
        ),
        _present(
            cpu_cores=host.cores,
            mem_total_mb=None if host.mem_total is None else host.mem_total // _MIB,
            disk_total_gb=None if host.disk_total is None else round(host.disk_total / _GIB, 2),
        ),
    )

    for name, container in snapshot.containers.items():
      if container.status != "running":
        continue
      before = previous.containers.get(name) if previous else None
      if before is not None and before.started_at != container.started_at:
        before = None  # a different run of the container: its counters started again
      peaks = _present(
          # Docker's percentage counts a whole core as 100, so it is shared over the cores.
          cpu_pct=(None if container.cpu_percent is None or not host.cores
                   else round(container.cpu_percent / host.cores, 2)),
          mem_used_mb=None if container.mem_bytes is None else container.mem_bytes // _MIB,
          net_rx_kbps=_rate_kbps(container.net_rx_bytes, before and before.net_rx_bytes, seconds),
          net_tx_kbps=_rate_kbps(container.net_tx_bytes, before and before.net_tx_bytes, seconds),
      )
      if peaks:
        self.containers.setdefault(name, MinutePeaks()).fold(minute, peaks)

  def pending(self):
    """What has not been drained: the host's minutes, and by container name."""
    return self.host.snapshot(), {name: peaks.snapshot() for name, peaks in self.containers.items()}

  def idle(self) -> bool:
    return not len(self.host) and not self.containers

  def drain(self, before: datetime):
    """Remove and return the completed minutes: those of the host, and by container name."""
    host = self.host.drain(before)
    containers = {}
    for name in list(self.containers):
      minutes = self.containers[name].drain(before)
      if minutes:
        containers[name] = minutes
      if not len(self.containers[name]):
        del self.containers[name]
    return host, containers


class Metrics(Base):
  def __init__(self):
    super().__init__()

    self.db_path = config.DATA_DIR
    os.makedirs(f"{self.db_path}/metrics", exist_ok=True)

    self._dbs = {"metrics": {}}

    # In-memory per-minute peaks of the manager's own metrics: the background sampler folds
    # samples in, the minutely flush drains completed minutes to the shard. `_sampler_prev` is
    # the last CPU/net counter sample.
    self._peaks = MinutePeaks()
    self._sampler_prev = None
    self._sampler_stop = threading.Event()
    self._sampler_thread = threading.Thread(
        target=self._sample_loop, name="metrics-sampler", daemon=True)

    # The same for each worker, filled from its reports (`ingest`), drained by `flush_workers`.
    self._workers: dict[str, WorkerPeaks] = {}

  def start_sampler(self):
    self._sampler_thread.start()

  def get_metrics_db(self, hostname):
    """
    1 database per hostname
    """
    with self.lock:
      if hostname not in self._dbs["metrics"]:
        path = f"{self.db_path}/metrics/{hostname}.db"
        db = SqliteDatabase(
            path,
            pragmas={
                "journal_mode": "wal",
                "cache_size": -2000,
                "foreign_keys": 1,
                "busy_timeout": 5000,
            },
        )

        class BaseModel(Model):
          class Meta:
            database = db

        class WorkerMeta(BaseModel):
          id = IntegerField(primary_key=True)  # always row id=1
          cpu_cores = IntegerField()
          mem_total_mb = IntegerField()
          disk_total_gb = FloatField()

        class WorkerMetrics(BaseModel):
          ts = DateTimeField(primary_key=True)
          cpu_pct = FloatField()
          mem_used_mb = IntegerField()
          mem_cached_mb = IntegerField()
          disk_used_gb = FloatField()
          load_avg_1m = FloatField(null=True)
          load_avg_5m = FloatField(null=True)
          load_avg_15m = FloatField(null=True)
          net_rx_kbps = FloatField()
          net_tx_kbps = FloatField()

        class ContainerMetrics(BaseModel):
          name = CharField()
          ts = DateTimeField()
          cpu_pct = FloatField()
          mem_used_mb = FloatField()
          net_rx_kbps = FloatField()
          net_tx_kbps = FloatField()

          class Meta:
            primary_key = CompositeKey("name", "ts")

        db.connect(reuse_if_open=True)
        db.create_tables([WorkerMeta, WorkerMetrics, ContainerMetrics], safe=True)
        self._dbs["metrics"][hostname] = {
            "db": db,
            "models": {
                "WorkerMeta": WorkerMeta,
                "WorkerMetrics": WorkerMetrics,
                "ContainerMetrics": ContainerMetrics,
            },
        }

    return self._dbs["metrics"][hostname]

  def ingest(self, hostname: str, snapshots: list[Snapshot]):
    """Add the snapshots of a worker's report to its per-minute peaks, in memory. The report
    route calls this; `flush_worker` writes the completed minutes."""
    with self.lock:
      self._workers.setdefault(hostname, WorkerPeaks())
      for snapshot in snapshots:
        self._workers[hostname].fold(snapshot)

  def flush_workers(self):
    """Write the completed minutes of every worker that has reported."""
    with self.lock:
      hostnames = list(self._workers)
    for hostname in hostnames:
      self.flush_worker(hostname)

  def flush_worker(self, hostname: str):
    """Write a worker's completed minutes to its shard. The minute in progress keeps
    accumulating; a minute with no reports simply gets no row. A worker that has gone
    quiet is dropped once everything is written."""
    now_minute = utcnow().replace(second=0, microsecond=0)
    with self.lock:
      pending = self._workers.get(hostname)
      host_minutes, container_minutes = pending.drain(now_minute) if pending else ({}, {})
      if pending is not None and pending.idle():
        del self._workers[hostname]

    if not host_minutes and not container_minutes:
      return

    db_info = self.get_metrics_db(hostname)
    WorkerMeta = db_info["models"]["WorkerMeta"]
    WorkerMetrics = db_info["models"]["WorkerMetrics"]
    ContainerMetrics = db_info["models"]["ContainerMetrics"]

    with db_info["db"].atomic():
      rows = [row for minute, peak in sorted(host_minutes.items()) if (row := _host_row(minute, peak))]
      for row in rows:
        WorkerMetrics.replace(**row).execute()

      sized = [peak for _, peak in sorted(host_minutes.items())
               if all(key in peak for key in ("cpu_cores", "mem_total_mb", "disk_total_gb"))]
      if sized:
        WorkerMeta.replace(
            id=1,
            cpu_cores=sized[-1]["cpu_cores"],
            mem_total_mb=sized[-1]["mem_total_mb"],
            disk_total_gb=sized[-1]["disk_total_gb"],
        ).execute()

      for name, minutes in container_minutes.items():
        for minute in sorted(minutes):
          ContainerMetrics.replace(**_container_row(name, minute, minutes[minute])).execute()

    logger.debug(f"Flushed metrics for {hostname}: {len(rows)} minute(s), {len(container_minutes)} container(s).")

  def _pending(self, hostname: str):
    """The minutes of a host not yet written to its shard, in memory: its own, and by container name."""
    with self.lock:
      if hostname == get_env("TS_NAME"):
        return self._peaks.snapshot(), {}
      workers = self._workers.get(hostname)
      return workers.pending() if workers else ({}, {})

  def forget(self, hostname: str):
    """Drop what a removed worker has not yet flushed."""
    with self.lock:
      self._workers.pop(hostname, None)

  def _sample_loop(self):
    """Background thread: read the manager container's own cgroup/net/disk usage
    every few seconds and fold it into the current minute's peak. CPU is
    normalized against the container's CPU allowance (cgroup quota if limited,
    else host cores); memory total is the cgroup limit if set, else host memory;
    disk is the root filesystem so temp usage counts. The minutely flush
    (`collect_self`) drains it to the shard."""
    while not self._sampler_stop.wait(config.METRICS_SAMPLE_INTERVAL):
      try:
        now = time.monotonic()
        minute = utcnow().replace(second=0, microsecond=0)

        cpu_usage_usec = next(
            int(line.split()[1])
            for line in (config.CGROUP_ROOT / "cpu.stat").read_text().splitlines()
            if line.startswith("usage_usec ")
        )
        interfaces = [
            line.split()
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]
            if ":" in line
        ]
        rx_bytes = sum(int(f[1]) for f in interfaces if not f[0].startswith("lo:"))
        tx_bytes = sum(int(f[9]) for f in interfaces if not f[0].startswith("lo:"))

        prev, self._sampler_prev = self._sampler_prev, (cpu_usage_usec, rx_bytes, tx_bytes, now)
        if prev is None:
          continue  # no previous counter for a rate -> seed only, skip this point
        elapsed = now - prev[3]

        cpu_max = (config.CGROUP_ROOT / "cpu.max").read_text().split()
        cpu_allowance = (os.cpu_count() or 1) if cpu_max[0] == "max" else int(cpu_max[0]) / int(cpu_max[1])
        cpu_pct = round(max(cpu_usage_usec - prev[0], 0) / (elapsed * 1_000_000) / cpu_allowance * 100, 2)
        net_rx_kbps = round(max(rx_bytes - prev[1], 0) / 1024 / elapsed, 2)
        net_tx_kbps = round(max(tx_bytes - prev[2], 0) / 1024 / elapsed, 2)

        mem_used = int((config.CGROUP_ROOT / "memory.current").read_text())
        mem_stat = dict(
            line.split(maxsplit=1) for line in (config.CGROUP_ROOT / "memory.stat").read_text().splitlines()
        )
        mem_cached = int(mem_stat.get("file", 0))
        mem_max = (config.CGROUP_ROOT / "memory.max").read_text().strip()
        if mem_max == "max":
          mem_total = next(
              int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines()
              if line.startswith("MemTotal:")
          )
        else:
          mem_total = int(mem_max)

        disk = shutil.disk_usage("/")

        with self.lock:
          self._peaks.fold(
              minute,
              {
                  "cpu_pct": cpu_pct,
                  "mem_used_mb": mem_used // _MIB,
                  "mem_cached_mb": mem_cached // _MIB,
                  "disk_used_gb": round(disk.used / _GIB, 2),
                  "net_rx_kbps": net_rx_kbps,
                  "net_tx_kbps": net_tx_kbps,
              },
              {
                  "cpu_cores": round(cpu_allowance),
                  "mem_total_mb": mem_total // _MIB,
                  "disk_total_gb": round(disk.total / _GIB, 2),
              },
          )
      except Exception:
        logger.warning("Manager metrics sample failed.", exc_info=True)

  def collect_self(self):
    """Flush completed minutes of the manager's peak accumulator to its shard.
    The in-progress minute is left to keep accumulating; a minute with no samples
    (e.g. spanning a restart) simply gets no row."""
    hostname = get_env("TS_NAME")
    now_minute = utcnow().replace(second=0, microsecond=0)

    with self.lock:
      completed = self._peaks.drain(now_minute)

    if not completed:
      return

    db_info = self.get_metrics_db(hostname)
    WorkerMeta = db_info["models"]["WorkerMeta"]
    WorkerMetrics = db_info["models"]["WorkerMetrics"]

    latest = completed[max(completed)]
    WorkerMeta.replace(
        id=1,
        cpu_cores=latest["cpu_cores"],
        mem_total_mb=latest["mem_total_mb"],
        disk_total_gb=latest["disk_total_gb"],
    ).execute()

    for minute in sorted(completed):
      WorkerMetrics.replace(**_host_row(minute, completed[minute])).execute()

    logger.info(f"Flushed manager metrics for {hostname}: {len(completed)} minute(s).")

  def _drop_shard(self, kind: str, key: str):
    """Close the cached connection for a shard and delete its files (`.db` plus
    the `-wal`/`-shm` siblings). Removing the file is how space for a deleted
    host/container is reclaimed -- its pages are never reused, so a row delete
    or VACUUM would not."""
    with self.lock:
      entry = self._dbs[kind].pop(key, None)
    if entry is not None:
      try:
        entry["db"].close()
      except Exception:
        logger.warning(f"Failed to close {kind} shard '{key}' before removal.", exc_info=True)
    db_file = f"{self.db_path}/{kind}/{key}.db"
    for path in (db_file, f"{db_file}-wal", f"{db_file}-shm"):
      Path(path).unlink(missing_ok=True)

  def _prune_orphan_shards(self, kind: str, live_keys: set) -> int:
    """Delete shard files whose key is not in live_keys. Returns the count."""
    shard_dir = Path(self.db_path) / kind
    orphans = {path.stem for path in shard_dir.glob("*.db")} - live_keys
    for key in sorted(orphans):
      self._drop_shard(kind, key)
      logger.info(f"Removed orphaned {kind} shard '{key}'.")
    return len(orphans)

  def cleanup(self, days: int = config.RETENTION_DAYS):
    cutoff = utcnow() - timedelta(days=days)

    # Drop shards for hosts that no longer exist (keyed by hostname -- manager
    # plus all workers, incl. offline).
    live_hosts = {get_env("TS_NAME")} | {worker.hostname for worker in Worker.select()}
    dropped = self._prune_orphan_shards("metrics", live_hosts)

    # Delete rows past the retention window; freed pages are reused by later
    # inserts, so shards plateau (no VACUUM). Skip shards not yet on disk.
    metrics_dir = Path(self.db_path) / "metrics"
    existing_hosts = {path.stem for path in metrics_dir.glob("*.db")} & live_hosts
    deleted_w = 0
    deleted_c = 0
    for hostname in sorted(existing_hosts):
      db_info = self.get_metrics_db(hostname)
      WorkerMetrics = db_info["models"]["WorkerMetrics"]
      ContainerMetrics = db_info["models"]["ContainerMetrics"]
      deleted_w += WorkerMetrics.delete().where(WorkerMetrics.ts < cutoff).execute()
      deleted_c += ContainerMetrics.delete().where(ContainerMetrics.ts < cutoff).execute()
    logger.info(
        f"Metrics cleanup: removed {deleted_w} worker rows, {deleted_c} container rows "
        f"older than {days} days; dropped {dropped} orphaned shard(s).")

  def query_period(self, hostname: str, period: str = "1h"):
    period_config = _PERIODS.get(period, _PERIODS["1h"])

    # Snap now to previous minute for consistent bucketing
    now = utcnow().replace(second=0, microsecond=0)
    since = now - period_config["delta"]

    # Get the db shard for this hostname
    db_info = self.get_metrics_db(hostname)
    WorkerMeta = db_info["models"]["WorkerMeta"]
    WorkerMetrics = db_info["models"]["WorkerMetrics"]
    ContainerMetrics = db_info["models"]["ContainerMetrics"]

    # The minutes not yet written are in memory; the newest of them, the one in progress, is the reading
    # that is current.
    pending_host, pending_containers = self._pending(hostname)
    by_minute = {row["ts"]: row for row in WorkerMetrics.select().where(WorkerMetrics.ts >= since).dicts()}
    for minute, peak in pending_host.items():
      if row := _host_row(minute, peak):
        by_minute[minute] = row
    rows = [by_minute[minute] for minute in sorted(by_minute)]

    # Format timestamps once — compute template with None values for missing fields
    metric_fields = {k for k in WorkerMetrics._meta.fields.keys() if k != "ts"}
    empty_row_template = {}
    for k in metric_fields:
      empty_row_template[k] = None

    for r in rows:
      r["ts"] = r["ts"].isoformat() + "Z"

    # Fill in missing buckets with ts (creates one-minute rows)
    rows = self._fill_missing_values(
        rows,
        now,
        period_config["bucket"] * period_config["points"],
        empty_row_template,
    )

    # Aggregate metrics into period buckets
    rows = self._aggregate_buckets(rows, period_config["bucket"], ["ts"])

    # Container series — group by name, same bucketing
    names = {c["name"] for c in ContainerMetrics.select(ContainerMetrics.name).where(ContainerMetrics.ts >= since).distinct().dicts()}
    names |= {name for name, minutes in pending_containers.items() if any(minute >= since for minute in minutes)}
    containers = [self.query_container_period(hostname, name, period) for name in sorted(names)]

    # Get static metadata for capacity context
    meta = WorkerMeta.select().dicts().first() or {}

    return {"host": rows, "containers": containers, "meta": meta}

  def _aggregate_buckets(self, rows, bucket, agg_fields):
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

  def _fill_missing_values(self, rows, start, target_points, template):
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

  def query_container_period(self, hostname: str, container: str, period: str = "1h"):
    period_config = _PERIODS.get(period, _PERIODS["1h"])

    # Snap now to previous minute for consistent bucketing
    now = utcnow().replace(second=0, microsecond=0)
    since = now - period_config["delta"]

    # Get the db shard for this hostname
    db_info = self.get_metrics_db(hostname)
    ContainerMetrics = db_info["models"]["ContainerMetrics"]

    by_minute = {
        row["ts"]: row
        for row in ContainerMetrics.select().where((ContainerMetrics.name == container) & (ContainerMetrics.ts >= since)).dicts()
    }
    for minute, peak in self._pending(hostname)[1].get(container, {}).items():
      by_minute[minute] = _container_row(container, minute, peak)
    rows = [by_minute[minute] for minute in sorted(by_minute)]

    # Format timestamps once — compute template with None values for missing fields
    metric_fields = {k for k in ContainerMetrics._meta.fields.keys() if k not in {"ts", "name"}}
    empty_row_template = {"name": container}
    for k in metric_fields:
      empty_row_template[k] = None

    for r in rows:
      r["ts"] = r["ts"].isoformat() + "Z"

    # Fill in missing buckets with ts (creates one-minute rows)
    rows = self._fill_missing_values(
        rows,
        now,
        period_config["bucket"] * period_config["points"],
        empty_row_template,
    )

    # Aggregate metrics into period buckets
    rows = self._aggregate_buckets(rows, period_config["bucket"], ["ts", "name"])

    return rows
