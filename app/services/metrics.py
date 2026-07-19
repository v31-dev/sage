import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from peewee import CharField, CompositeKey, DateTimeField, FloatField, IntegerField, Model, SqliteDatabase

from services.base import Base
from services.db import Worker
from utils.common import get_env

logger = logging.getLogger(__name__)

# delta is period start end range
# buckets is number of points to aggregate
# points is number of data points to return
# data in db is stored in minute intervals
_PERIODS = {
    "1m": {"delta": timedelta(minutes=1), "bucket": 1, "points": 1},
    "1h": {"delta": timedelta(hours=1), "bucket": 1, "points": 60},
    "24h": {"delta": timedelta(hours=24), "bucket": 24, "points": 60},
    "1w": {"delta": timedelta(days=7), "bucket": 168, "points": 60},
}

# The manager samples its own usage every few seconds and reports the per-minute peak
_CGROUP = Path("/sys/fs/cgroup")
_SAMPLE_INTERVAL = 5


class Metrics(Base):
  def __init__(self):
    super().__init__()

    self.db_path = "/app/data/metrics"
    os.makedirs(f"{self.db_path}/metrics", exist_ok=True)

    self._dbs = {"metrics": {}}

    # Reused across collects which also pools connections.
    self._http = httpx.Client(timeout=10)

    # In-memory per-minute peak accumulator for the manager's own metrics: the
    # background sampler folds samples in, the minutely flush drains completed
    # minutes to the shard. `_sampler_prev` is the last CPU/net counter sample.
    self._peaks = {}
    self._sampler_prev = None
    self._sampler_stop = threading.Event()
    self._sampler_thread = threading.Thread(
        target=self._sample_loop, name="metrics-sampler", daemon=True)
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

  def collect(self, ip, hostname):
    metrics_endpoint = f"http://{ip}:61208/api/4"
    plugins = ["cpu", "mem", "fs", "load", "network", "containers"]
    ts = datetime.now().replace(second=0, microsecond=0)

    try:
      data = {ep: self._http.get(f"{metrics_endpoint}/{ep}").json() for ep in plugins}

      root_fs = max(data["fs"], key=lambda f: f.get("size", 0))
      db_info = self.get_metrics_db(hostname)
      WorkerMeta = db_info["models"]["WorkerMeta"]
      WorkerMetrics = db_info["models"]["WorkerMetrics"]
      ContainerMetrics = db_info["models"]["ContainerMetrics"]

      WorkerMeta.replace(
          id=1,
          cpu_cores=data["cpu"]["cpucore"],
          mem_total_mb=data["mem"]["total"] // (1024**2),
          disk_total_gb=round(root_fs["size"] / (1024**3), 2),
      ).execute()

      WorkerMetrics.replace(
          ts=ts,
          cpu_pct=data["cpu"]["total"],
          mem_used_mb=data["mem"]["used"] // (1024**2),
          mem_cached_mb=data["mem"]["cached"] // (1024**2),
          disk_used_gb=round(root_fs["used"] / (1024**3), 2),
          load_avg_1m=data["load"]["min1"],
          load_avg_5m=data["load"]["min5"],
          load_avg_15m=data["load"]["min15"],
          net_rx_kbps=round(
              sum(
                  i.get("bytes_recv_rate_per_sec", 0)
                  for i in data["network"]
                  if i.get("interface_name") != "lo"
              )
              / 1024,
              2,
          ),
          net_tx_kbps=round(
              sum(
                  i.get("bytes_sent_rate_per_sec", 0)
                  for i in data["network"]
                  if i.get("interface_name") != "lo"
              )
              / 1024,
              2,
          ),
      ).execute()

      cpu_cores = data["cpu"]["cpucore"]
      for ct in data["containers"]:
        cpu_total = ct.get("cpu", {}).get("total", 0)
        mem_usage = ct.get("memory", {}).get("usage", 0)
        net_rx = 0
        net_tx = 0

        if cpu_cores > 0:
          cpu_total = round(cpu_total / cpu_cores, 2)

        if ct.get("network"):
          net_rx = round(ct["network"].get("rx", 0) / 1024, 2)
          net_tx = round(ct["network"].get("tx", 0) / 1024, 2)

        ContainerMetrics.replace(
            ts=ts,
            name=ct["name"],
            cpu_pct=cpu_total,
            mem_used_mb=mem_usage // (1024**2),
            net_rx_kbps=net_rx,
            net_tx_kbps=net_tx,
        ).execute()

      logger.info(f"Collected metrics for {hostname} ({len(data['containers'])} containers) at {ts.isoformat()}.")
    except Exception as e:
      error_name = type(e).__name__
      error_message = str(e).strip()
      error_detail = f"{error_name}: {error_message}" if error_message else error_name
      raise RuntimeError(f"Failed to collect metrics for {hostname} ({ip}): {error_detail}") from e

  def _sample_loop(self):
    """Background thread: read the manager container's own cgroup/net/disk usage
    every few seconds and fold it into the current minute's peak. CPU is
    normalized against the container's CPU allowance (cgroup quota if limited,
    else host cores); memory total is the cgroup limit if set, else host memory;
    disk is the root filesystem so temp usage counts. The minutely flush
    (`collect_self`) drains it to the shard."""
    while not self._sampler_stop.wait(_SAMPLE_INTERVAL):
      try:
        now = time.monotonic()
        minute = datetime.now().replace(second=0, microsecond=0)

        cpu_usage_usec = next(
            int(line.split()[1])
            for line in (_CGROUP / "cpu.stat").read_text().splitlines()
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

        cpu_max = (_CGROUP / "cpu.max").read_text().split()
        cpu_allowance = (os.cpu_count() or 1) if cpu_max[0] == "max" else int(cpu_max[0]) / int(cpu_max[1])
        cpu_pct = round(max(cpu_usage_usec - prev[0], 0) / (elapsed * 1_000_000) / cpu_allowance * 100, 2)
        net_rx_kbps = round(max(rx_bytes - prev[1], 0) / 1024 / elapsed, 2)
        net_tx_kbps = round(max(tx_bytes - prev[2], 0) / 1024 / elapsed, 2)

        mem_used = int((_CGROUP / "memory.current").read_text())
        mem_stat = dict(
            line.split(maxsplit=1) for line in (_CGROUP / "memory.stat").read_text().splitlines()
        )
        mem_cached = int(mem_stat.get("file", 0))
        mem_max = (_CGROUP / "memory.max").read_text().strip()
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
          # The minutely flush drains completed minutes, so this normally holds
          # one or two; bound it in case the flush ever stalls.
          cutoff = minute - timedelta(minutes=10)
          for stale in [m for m in self._peaks if m < cutoff]:
            del self._peaks[stale]

          peak = self._peaks.setdefault(minute, {
              "cpu_pct": 0.0, "mem_used_mb": 0, "mem_cached_mb": 0,
              "disk_used_gb": 0.0, "net_rx_kbps": 0.0, "net_tx_kbps": 0.0,
          })
          peak["cpu_pct"] = max(peak["cpu_pct"], cpu_pct)
          peak["mem_used_mb"] = max(peak["mem_used_mb"], mem_used // (1024**2))
          peak["mem_cached_mb"] = max(peak["mem_cached_mb"], mem_cached // (1024**2))
          peak["disk_used_gb"] = max(peak["disk_used_gb"], round(disk.used / (1024**3), 2))
          peak["net_rx_kbps"] = max(peak["net_rx_kbps"], net_rx_kbps)
          peak["net_tx_kbps"] = max(peak["net_tx_kbps"], net_tx_kbps)
          peak["cpu_cores"] = round(cpu_allowance)
          peak["mem_total_mb"] = mem_total // (1024**2)
          peak["disk_total_gb"] = round(disk.total / (1024**3), 2)
      except Exception:
        logger.warning("Manager metrics sample failed.", exc_info=True)

  def stop_sampler(self):
    self._sampler_stop.set()

  def collect_self(self):
    """Flush completed minutes of the manager's peak accumulator to its shard.
    The in-progress minute is left to keep accumulating; a minute with no samples
    (e.g. spanning a restart) simply gets no row."""
    hostname = get_env("HOSTNAME")
    now_minute = datetime.now().replace(second=0, microsecond=0)

    with self.lock:
      completed = {minute: peak for minute, peak in self._peaks.items() if minute < now_minute}
      for minute in completed:
        del self._peaks[minute]

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
      peak = completed[minute]
      WorkerMetrics.replace(
          ts=minute,
          cpu_pct=peak["cpu_pct"],
          mem_used_mb=peak["mem_used_mb"],
          mem_cached_mb=peak["mem_cached_mb"],
          disk_used_gb=peak["disk_used_gb"],
          load_avg_1m=None,
          load_avg_5m=None,
          load_avg_15m=None,
          net_rx_kbps=peak["net_rx_kbps"],
          net_tx_kbps=peak["net_tx_kbps"],
      ).execute()

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

  def cleanup(self, days: int = 7):
    cutoff = datetime.now() - timedelta(days=days)

    # Drop shards for hosts that no longer exist (keyed by hostname -- manager
    # plus all workers, incl. offline).
    live_hosts = {get_env("HOSTNAME")} | {worker.hostname for worker in Worker.select()}
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
    now = datetime.now().replace(second=0, microsecond=0)
    since = now - period_config["delta"]

    # Get the db shard for this hostname
    db_info = self.get_metrics_db(hostname)
    WorkerMeta = db_info["models"]["WorkerMeta"]
    WorkerMetrics = db_info["models"]["WorkerMetrics"]
    ContainerMetrics = db_info["models"]["ContainerMetrics"]

    rows = list(
        WorkerMetrics.select()
        .where(WorkerMetrics.ts >= since)
        .order_by(WorkerMetrics.ts)
        .dicts()
    )

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
    containers = list(
        ContainerMetrics.select(ContainerMetrics.name)
        .where(ContainerMetrics.ts >= since)
        .distinct()
        .dicts()
    )
    containers = [self.query_container_period(hostname, c["name"], period) for c in containers]

    # Get static metadata for capacity context
    meta = WorkerMeta.select().dicts().first() or {}

    return {"host": rows, "containers": containers, "meta": meta}

  def _aggregate_buckets(self, rows, bucket, agg_fields):
    """
    Aggregate rows into buckets by averaging values within each bucket time range.
    Aggregate all fields except for `agg_fields` fields.
    Aggregate functions will be average, if any bucket has missing values for a field, the aggregated value will be null.
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
      if idx >= 0 and rows[idx]["ts"] == current_iso:
        result.append(rows[idx])
        idx -= 1  # Move backwards through rows
      else:
        # Insert empty row for missing bucket
        result.append({"ts": current_iso, **template})
      current -= timedelta(minutes=1)

    return list(reversed(result))

  def query_container_period(self, hostname: str, container: str, period: str = "1h"):
    period_config = _PERIODS.get(period, _PERIODS["1h"])

    # Snap now to previous minute for consistent bucketing
    now = datetime.now().replace(second=0, microsecond=0)
    since = now - period_config["delta"]

    # Get the db shard for this hostname
    db_info = self.get_metrics_db(hostname)
    ContainerMetrics = db_info["models"]["ContainerMetrics"]

    rows = list(
        ContainerMetrics.select()
        .where((ContainerMetrics.name == container) & (ContainerMetrics.ts >= since))
        .order_by(ContainerMetrics.ts)
        .dicts()
    )

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
