"""The readings of a minute reduced to their peaks: each worker's from its reports, and the rows a minute is written as."""
from datetime import datetime, timedelta

import config
from services.worker_reports import Snapshot

MIB = 1024**2
GIB = 1024**3

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


def host_row(minute: datetime, peak: dict) -> dict | None:
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


def container_row(name: str, minute: datetime, peak: dict) -> dict:
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
            mem_used_mb=None if host.mem_used is None else host.mem_used // MIB,
            mem_cached_mb=None if host.mem_cached is None else host.mem_cached // MIB,
            disk_used_gb=None if host.disk_used is None else round(host.disk_used / GIB, 2),
            load_avg_1m=host.load1,
            load_avg_5m=host.load5,
            load_avg_15m=host.load15,
            net_rx_kbps=rx,
            net_tx_kbps=tx,
        ),
        _present(
            cpu_cores=host.cores,
            mem_total_mb=None if host.mem_total is None else host.mem_total // MIB,
            disk_total_gb=None if host.disk_total is None else round(host.disk_total / GIB, 2),
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
          mem_used_mb=None if container.mem_bytes is None else container.mem_bytes // MIB,
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
