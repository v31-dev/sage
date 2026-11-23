"""Each worker's state as its pages show it, worked out from the facts its owners hold; the setup outcomes the
Manager's convergence leaves, the certificates the worker applied, and the stamps Sage keeps on the worker."""
from dataclasses import asdict, dataclass
from datetime import datetime

import config
from services.base import Base
from services.certs import TEMPLATE_DIGEST, Certs
from services.cloudflare import Cloudflare
from services.db.models import Worker
from services.domains.updates import callers_observed
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from services.workers.decisions import WorkerFacts, WorkerReport, WorkerView, addressable, decide_worker, report_of, workable
from services.workers.rendering import applications_memory_mb
from utils.common import utcnow


class Workers(Base):
  """Each worker's setup outcome since the Manager started: succeeded, failed or refused (another Sage owns it); None
  until a setup has run, and again once its SSH server stops answering, since it is set up again when it returns. And
  the stamps on the worker: one line each under `revisions/`, its owner (`<instance id> <domain>`) and the fingerprint
  of the infrastructure applied. And the proofs of the certificates it applied, by role, from its `certs` stamp at its
  setup and from each distribution; none until one of them. And its applications' networks as its newest listing
  found them; none until one."""

  def __init__(self):
    super().__init__()
    # By hostname: the outcome and the address the setup applied.
    self._setups: dict[str, tuple[str, str | None]] = {}
    self._certificates: dict[str, dict[str, str]] = {}
    # By hostname: when the newest listing of its applications' networks started, and their subnets by container id.
    self._networks: dict[str, tuple[datetime, dict[str, tuple[str, ...]]]] = {}

  async def read_revisions(self, hostname: str) -> dict[str, str]:
    """The worker's stamps by name; none when it has none yet."""
    _, lines = await Tailscale().exec_command(
        hostname, f"grep -H . {config.WORKER_HOME_DIR}/revisions/* 2>/dev/null || true", timeout=30)
    revisions = {}
    for line in lines:
      path, separator, value = line.partition(":")
      if separator and value.strip():
        revisions[path.rsplit("/", 1)[-1]] = value.strip()
    return revisions

  async def write_revision(self, hostname: str, name: str, value: str):
    await Tailscale().sync_file(hostname, config.TEMPLATES_DIR / "worker/file", f"{config.WORKER_HOME_DIR}/revisions/{name}",
                                {"CONTENT": value})

  def setup(self, hostname: str) -> str | None:
    with self.lock:
      return self._setups.get(hostname, (None, None))[0]

  def setup_address(self, hostname: str) -> str | None:
    """The worker's address its last setup applied."""
    with self.lock:
      return self._setups.get(hostname, (None, None))[1]

  def record_setup(self, hostname: str, outcome: str | None, address: str | None = None):
    with self.lock:
      if outcome is None:
        self._setups.pop(hostname, None)
      else:
        self._setups[hostname] = (outcome, address)

  def certificates(self, hostname: str) -> dict[str, str] | None:
    with self.lock:
      return self._certificates.get(hostname)

  def record_certificates(self, hostname: str, proofs: dict[str, str]):
    with self.lock:
      self._certificates[hostname] = dict(proofs)

  def networks(self, hostname: str) -> dict[str, tuple[str, ...]] | None:
    """The subnets of each of the worker's applications' networks, by its container's id; None until a listing."""
    with self.lock:
      held = self._networks.get(hostname)
    return dict(held[1]) if held is not None else None

  def record_networks(self, hostname: str, subnets: dict[str, tuple[str, ...]], listed_at: datetime):
    """Holds a listing of the worker's applications' networks that started at `listed_at`, unless a newer one is held,
    since its recorders share no scope; tells the pages of the Domains they concern when they changed."""
    with self.lock:
      held = self._networks.get(hostname)
      if held is not None and held[0] > listed_at:
        return
      changed = held is None or held[1] != subnets
      self._networks[hostname] = (listed_at, dict(subnets))
    if changed:
      callers_observed(hostname)

  def forget(self, hostname: str):
    """Drops what is held of the worker once it is removed."""
    with self.lock:
      self._setups.pop(hostname, None)
      self._certificates.pop(hostname, None)
      self._networks.pop(hostname, None)

  def clear(self):
    """Drops what is held of every worker, as when the Manager starts: after a platform restore."""
    with self.lock:
      self._setups.clear()
      self._certificates.clear()
      self._networks.clear()


@dataclass(frozen=True)
class WorkerState:
  """The decided view with the facts it was decided from, the worker's address and path in the tailnet, its two
  certificates (`worker`, `discovery`): whether each covers the names it needs, whether the worker applied it, and its
  expiry and error; and its tunnel's health as known now (`Cloudflare.tunnel`). Certificates stay out of its readiness,
  and the tunnel decides only how it answers (`connectivity`)."""
  view: WorkerView
  ssh: str
  setup: str | None
  report: WorkerReport
  tasks: frozenset[str]
  address: str | None
  path: str | None
  certificates: dict
  tunnel: str | None
  removing: bool = False

  @property
  def workable(self) -> bool:
    return workable(self.ssh, self.setup, self.removing)

  @property
  def addressable(self) -> bool:
    return addressable(self.ssh, self.address)


def is_workable(hostname: str) -> bool:
  """Whether Sage can act on the worker now (`workable`); not when it is no longer configured."""
  worker = Worker.get_or_none(Worker.hostname == hostname)
  return worker is not None and workable(Tailscale().ssh(hostname), Workers().setup(hostname), worker.removing)


def is_addressable(hostname: str) -> bool:
  """Whether the worker answers at an address in the tailnet now (`addressable`)."""
  return addressable(Tailscale().ssh(hostname), Tailscale().address(hostname))


def worker_state(hostname: str, subdomain: str, tasks: frozenset[str], removing: bool = False) -> WorkerState:
  """The worker's state now, given its subdomain, the worker tasks queued or running on it and its removal mark."""
  now = utcnow()
  ssh, setup = Tailscale().ssh(hostname), Workers().setup(hostname)
  tunnel = Cloudflare().tunnel(Cloudflare().worker_tunnel_name(hostname))
  report = report_of(WorkerReports().latest(hostname), now)
  view = decide_worker(WorkerFacts(ssh, setup, report, tasks, (now - WorkerReports().started).total_seconds(), tunnel,
                                   removing))
  address = Tailscale().address(hostname)
  return WorkerState(view, ssh, setup, report, tasks, address, Tailscale().path(address) if address else None,
                     Certs().worker_state(hostname, subdomain, Workers().certificates(hostname), TEMPLATE_DIGEST),
                     tunnel, removing)


def worker_payload(worker: Worker, state: WorkerState, tunnel_name: str, metrics: dict | None) -> dict:
  """The worker as its pages show it: its configuration, its decided state with the facts it was decided from (its SSH
  server, its setup, its report, the worker tasks under way), where it is in the tailnet, its certificates, its
  tunnel's health and name, and its newest minute of metrics."""
  return {
      "hostname": worker.hostname,
      "subdomain": worker.subdomain,
      "view": {**asdict(state.view), "ready": state.view.ready},
      "ssh": state.ssh,
      "setup": state.setup,
      "report": asdict(state.report),
      "tasks": sorted(state.tasks),
      "address": state.address,
      "path": state.path,
      "certificates": state.certificates,
      "tunnel": state.tunnel,
      "tunnel_name": tunnel_name,
      "metrics": metrics,
      "memory": None if metrics is None or metrics.get("mem_total_mb") is None else {
          "total_mb": metrics["mem_total_mb"], "reserved_mb": config.WORKER_MEMORY_RESERVE_MB,
          "applications_mb": applications_memory_mb(metrics["mem_total_mb"])},
      "created_at": worker.created_at,
      "updated_at": worker.updated_at,
  }
