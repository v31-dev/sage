import logging
import os
import threading
from datetime import timedelta
from pathlib import Path

import config
from services.base import Base
from services.db.models import Worker
from services.updates import Updates, UpdateSource
from services.worker_reports import Snapshot
from utils.common import get_env, utcnow

from .peaks import MinutePeaks, WorkerPeaks, container_row, host_row
from .sampler import read_counters, read_sample
from .series import PERIODS, aggregate_buckets, fill_missing_values
from .shards import open_shard

logger = logging.getLogger(__name__)


def metrics_scope(part: str | None = None) -> str:
  """The queue's scope of the metric files; with a part (`flush`), of that work on them."""
  return f"metrics:{part}" if part else "metrics"


class Metrics(Base):
  """Each host's metrics: what the workers' reports and the manager's own sampler (`sampler`) read, held as the peaks of
  each minute (`peaks`) until the minute ends, then written to the host's file (`shards`), and read back as the graphs'
  series (`series`)."""

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

    # The same for each worker, filled from its reports (`ingest`), drained by `flush`; and each worker's newest
    # written minute, which the worker list shows.
    self._workers: dict[str, WorkerPeaks] = {}
    self._newest: dict[str, dict] = {}

  def start_sampler(self):
    self._sampler_thread.start()

  def get_metrics_db(self, hostname):
    """
    1 database per hostname
    """
    with self.lock:
      if hostname not in self._dbs["metrics"]:
        self._dbs["metrics"][hostname] = open_shard(f"{self.db_path}/metrics/{hostname}.db")

    return self._dbs["metrics"][hostname]

  def ingest(self, hostname: str, snapshots: list[Snapshot]):
    """Add the snapshots of a worker's report to its per-minute peaks, in memory. The report
    route calls this; `flush_worker` writes the completed minutes."""
    with self.lock:
      self._workers.setdefault(hostname, WorkerPeaks())
      for snapshot in snapshots:
        self._workers[hostname].fold(snapshot)

  def flush(self):
    """Writes the completed minutes of the manager's own metrics and of each worker's. Each worker's newest written
    minute is kept (`newest`) and dropped by a flush that writes none for it, so a worker gone quiet shows none; the
    worker list's sources fire for each worker whose newest minute changed."""
    self.collect_self()
    with self.lock:
      hostnames = sorted(self._workers.keys() | self._newest.keys())
    changed = []
    for hostname in hostnames:
      minute = self.flush_worker(hostname)
      with self.lock:
        if minute != self._newest.get(hostname):
          changed.append(hostname)
          if minute is None:
            del self._newest[hostname]
          else:
            self._newest[hostname] = minute
    if changed:
      Updates().changed(UpdateSource.WORKER_LIST)
    for hostname in changed:
      Updates().changed(UpdateSource.WORKER_ENTRY, hostname)

  def newest(self, hostname: str) -> dict | None:
    """The worker's newest written minute with the machine's size (`flush_worker`); None while it has none."""
    with self.lock:
      minute = self._newest.get(hostname)
      return dict(minute) if minute is not None else None

  def flush_worker(self, hostname: str) -> dict | None:
    """Write a worker's completed minutes to its shard. The minute in progress keeps
    accumulating; a minute with no reports simply gets no row. A worker that has gone
    quiet is dropped once everything is written. Returns the newest host row written, with the machine's size
    (`cpu_cores`, `mem_total_mb`, `disk_total_gb`, None when no minute carried it), or None."""
    now_minute = utcnow().replace(second=0, microsecond=0)
    with self.lock:
      pending = self._workers.get(hostname)
      host_minutes, container_minutes = pending.drain(now_minute) if pending else ({}, {})
      if pending is not None and pending.idle():
        del self._workers[hostname]

    if not host_minutes and not container_minutes:
      return None

    db_info = self.get_metrics_db(hostname)
    WorkerMeta = db_info["models"]["WorkerMeta"]
    WorkerMetrics = db_info["models"]["WorkerMetrics"]
    ContainerMetrics = db_info["models"]["ContainerMetrics"]

    with db_info["db"].atomic():
      rows = [row for minute, peak in sorted(host_minutes.items()) if (row := host_row(minute, peak))]
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
          ContainerMetrics.replace(**container_row(name, minute, minutes[minute])).execute()

    logger.debug(f"Flushed metrics for {hostname}: {len(rows)} minute(s), {len(container_minutes)} container(s).")
    if not rows:
      return None
    size = sized[-1] if sized else {}
    return {**rows[-1], **{key: size.get(key) for key in ("cpu_cores", "mem_total_mb", "disk_total_gb")}}

  def _pending(self, hostname: str):
    """The minutes of a host not yet written to its shard, in memory: its own, and by container name."""
    with self.lock:
      if hostname == get_env("TS_NAME"):
        return self._peaks.snapshot(), {}
      workers = self._workers.get(hostname)
      return workers.pending() if workers else ({}, {})

  def forget(self, hostname: str):
    """Drop what a removed worker has not yet flushed, and its newest minute."""
    with self.lock:
      self._workers.pop(hostname, None)
      self._newest.pop(hostname, None)

  def _sample_loop(self):
    """Background thread: read the manager container's own usage every few seconds (`sampler`) and fold it into the
    current minute's peak. The minutely flush (`collect_self`) drains it to the shard."""
    while not self._sampler_stop.wait(config.METRICS_SAMPLE_INTERVAL):
      try:
        minute = utcnow().replace(second=0, microsecond=0)
        prev, self._sampler_prev = self._sampler_prev, read_counters()
        if prev is None:
          continue  # no previous counter for a rate -> seed only, skip this point
        peaks, latest = read_sample(prev, self._sampler_prev)
        with self.lock:
          self._peaks.fold(minute, peaks, latest)
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
      WorkerMetrics.replace(**host_row(minute, completed[minute])).execute()

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
    period_config = PERIODS.get(period, PERIODS["1h"])

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
      if row := host_row(minute, peak):
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
    rows = fill_missing_values(
        rows,
        now,
        period_config["bucket"] * period_config["points"],
        empty_row_template,
    )

    # Aggregate metrics into period buckets
    rows = aggregate_buckets(rows, period_config["bucket"], ["ts"])

    # Container series — group by name, same bucketing
    names = {c["name"] for c in ContainerMetrics.select(ContainerMetrics.name).where(ContainerMetrics.ts >= since).distinct().dicts()}
    names |= {name for name, minutes in pending_containers.items() if any(minute >= since for minute in minutes)}
    containers = [self.query_container_period(hostname, name, period) for name in sorted(names)]

    # Get static metadata for capacity context
    meta = WorkerMeta.select().dicts().first() or {}

    return {"host": rows, "containers": containers, "meta": meta}

  def query_container_period(self, hostname: str, container: str, period: str = "1h"):
    period_config = PERIODS.get(period, PERIODS["1h"])

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
      by_minute[minute] = container_row(container, minute, peak)
    rows = [by_minute[minute] for minute in sorted(by_minute)]

    # Format timestamps once — compute template with None values for missing fields
    metric_fields = {k for k in ContainerMetrics._meta.fields.keys() if k not in {"ts", "name"}}
    empty_row_template = {"name": container}
    for k in metric_fields:
      empty_row_template[k] = None

    for r in rows:
      r["ts"] = r["ts"].isoformat() + "Z"

    # Fill in missing buckets with ts (creates one-minute rows)
    rows = fill_missing_values(
        rows,
        now,
        period_config["bucket"] * period_config["points"],
        empty_row_template,
    )

    # Aggregate metrics into period buckets
    rows = aggregate_buckets(rows, period_config["bucket"], ["ts", "name"])

    return rows
