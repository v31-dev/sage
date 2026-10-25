import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from services.base import Base
from utils.common import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContainerReading:
  """One container as Telegraf saw it. Times are nanoseconds since the epoch (the worker's
  clock); `finished_at` is absent for a container that never stopped."""
  name: str
  status: str
  image: str
  deployed_at: str | None
  exit_code: int | None
  oom_killed: bool
  started_at: int | None
  finished_at: int | None
  health: str | None
  cpu_percent: float | None
  mem_bytes: int | None
  net_rx_bytes: int | None
  net_tx_bytes: int | None


@dataclass(frozen=True)
class HostReading:
  """The worker host. Network bytes are counters summed over the physical interfaces."""
  cpu_idle_percent: float | None
  cores: int | None
  mem_total: int | None
  mem_used: int | None
  mem_cached: int | None
  mem_available: int | None
  load1: float | None
  load5: float | None
  load15: float | None
  disk_total: int | None
  disk_used: int | None
  net_rx_bytes: int | None
  net_tx_bytes: int | None


@dataclass(frozen=True)
class Snapshot:
  """Everything gathered in one interval. `at` is the worker's clock; `traefik_reachable` is
  None when the report carried no check."""
  at: datetime
  host: HostReading
  containers: dict[str, ContainerReading]
  traefik_reachable: bool | None


@dataclass(frozen=True)
class Received:
  """The newest snapshot of a worker and when the manager received it (the manager's clock)."""
  arrived_at: datetime
  snapshot: Snapshot


def _number(value):
  return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _integer(value):
  value = _number(value)
  return None if value is None else int(value)


def _sum(values):
  values = [value for value in values if value is not None]
  return sum(values) if values else None


def _host(metrics) -> HostReading:
  by_name = {}
  for metric in metrics:
    by_name.setdefault(metric["name"], []).append(metric)

  def fields(name):
    return by_name[name][0]["fields"] if name in by_name else {}

  interfaces = [m["fields"] for m in by_name.get("net", []) if m["tags"].get("interface") != "all"]
  return HostReading(
      cpu_idle_percent=_number(fields("cpu").get("usage_idle")),
      cores=_integer(fields("system").get("n_cpus")),
      mem_total=_integer(fields("mem").get("total")),
      mem_used=_integer(fields("mem").get("used")),
      mem_cached=_integer(fields("mem").get("cached")),
      mem_available=_integer(fields("mem").get("available")),
      load1=_number(fields("system").get("load1")),
      load5=_number(fields("system").get("load5")),
      load15=_number(fields("system").get("load15")),
      disk_total=_integer(fields("disk").get("total")),
      disk_used=_integer(fields("disk").get("used")),
      net_rx_bytes=_sum(_integer(i.get("bytes_recv")) for i in interfaces),
      net_tx_bytes=_sum(_integer(i.get("bytes_sent")) for i in interfaces),
  )


def _field(measurements, measurement, key):
  return measurements[measurement]["fields"].get(key) if measurement in measurements else None


def _containers(metrics) -> dict[str, ContainerReading]:
  by_container = {}
  for metric in metrics:
    if metric["name"].startswith("docker_container_") and "container_name" in metric["tags"]:
      by_container.setdefault(metric["tags"]["container_name"], {})[metric["name"]] = metric

  containers = {}
  for name, measurements in by_container.items():
    status = measurements.get("docker_container_status")
    if status is None:
      continue
    fields = status["fields"]
    tags = status["tags"]
    containers[name] = ContainerReading(
        name=name,
        status=str(tags.get("container_status", "")),
        image=str(tags.get("container_image", "")),
        deployed_at=tags.get("sage.deployed_at"),
        exit_code=_integer(fields.get("exitcode")),
        oom_killed=fields.get("oomkilled") is True,
        started_at=_integer(fields.get("started_at")),
        finished_at=_integer(fields.get("finished_at")),
        health=_field(measurements, "docker_container_health", "health_status"),
        cpu_percent=_number(_field(measurements, "docker_container_cpu", "usage_percent")),
        mem_bytes=_integer(_field(measurements, "docker_container_mem", "usage")),
        net_rx_bytes=_integer(_field(measurements, "docker_container_net", "rx_bytes")),
        net_tx_bytes=_integer(_field(measurements, "docker_container_net", "tx_bytes")),
    )
  return containers


def parse_batch(batch) -> list[Snapshot]:
  """The snapshots in a Telegraf batch (the JSON of the `http` output), oldest first. A batch
  holds one snapshot when everything is live and several after an outage of the manager.

  The metrics of one gather carry timestamps a second or two apart (the Docker input takes a
  while), so a snapshot is every metric in the same interval-sized bucket of the worker's clock.
  Raises ValueError for a batch that is not Telegraf's JSON."""
  if not isinstance(batch, dict) or not isinstance(batch.get("metrics"), list):
    raise ValueError("expected an object with a list of metrics")

  bucket_ms = config.REPORT_INTERVAL_SECONDS * 1000
  buckets: dict[int, list] = {}
  for metric in batch["metrics"]:
    if (not isinstance(metric, dict) or not isinstance(metric.get("name"), str)
            or not isinstance(metric.get("tags"), dict) or not isinstance(metric.get("fields"), dict)
            or _integer(metric.get("timestamp")) is None):
      raise ValueError("a metric needs a name, tags, fields and a timestamp")
    buckets.setdefault(int(metric["timestamp"]) // bucket_ms, []).append(metric)

  snapshots = []
  for bucket in sorted(buckets):
    metrics = buckets[bucket]
    check = next((m for m in metrics if m["name"] == "net_response"), None)
    snapshots.append(Snapshot(
        at=datetime.fromtimestamp(min(m["timestamp"] for m in metrics) / 1000, tz=timezone.utc).replace(tzinfo=None),
        host=_host(metrics),
        containers=_containers(metrics),
        traefik_reachable=None if check is None else check["fields"].get("result_code") == 0,
    ))
  return snapshots


class Reports(Base):
  """The newest snapshot Telegraf reported for each worker, in memory. The report route fills
  it and decides nothing; the operation queue reads it (see todo/06)."""

  def __init__(self):
    super().__init__()
    self._latest: dict[str, Received] = {}
    self._unknown: set[str] = set()

  def accept(self, hostname: str, snapshots: list[Snapshot]) -> bool:
    """Keep the newest snapshot of the batch. A batch that is older than what is held (a replay
    arriving late) is ignored. Returns True when this is the first report of the worker, or the
    first after a silence of a few intervals."""
    if not snapshots:
      return False
    newest = snapshots[-1]
    now = utcnow()
    with self.lock:
      current = self._latest.get(hostname)
      if current is not None and newest.at < current.snapshot.at:
        return False
      self._latest[hostname] = Received(now, newest)
    silence = config.REPORT_INTERVAL_SECONDS * 3
    return current is None or (now - current.arrived_at).total_seconds() > silence

  def first_unknown(self, hostname: str) -> bool:
    """True the first time a name that is not a worker sends a report, so it is logged once."""
    with self.lock:
      if hostname in self._unknown:
        return False
      self._unknown.add(hostname)
      return True

  def latest(self, hostname: str) -> Received | None:
    with self.lock:
      return self._latest.get(hostname)

  def forget(self, hostname: str):
    with self.lock:
      self._latest.pop(hostname, None)
