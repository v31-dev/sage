"""Each worker's state as its pages show it, worked out from the facts its owners hold; the setup outcomes the
Manager's convergence leaves, and the stamps Sage keeps on the worker."""
from dataclasses import dataclass

import config
from services.base import Base
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from services.workers.decisions import WorkerFacts, WorkerReport, WorkerView, decide_worker, report_of
from utils.common import utcnow


class Workers(Base):
  """Each worker's setup outcome since the Manager started: succeeded, failed or refused (another Sage owns it); None
  until a setup has run, and again once its SSH server stops answering, since it is set up again when it returns. And
  the stamps on the worker: one line each under `revisions/`, its owner (`<instance id> <domain>`) and the fingerprint
  of the infrastructure applied."""

  def __init__(self):
    super().__init__()
    # By hostname: the outcome and the address the setup applied.
    self._setups: dict[str, tuple[str, str | None]] = {}

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


@dataclass(frozen=True)
class WorkerState:
  """The decided view with the facts it was decided from, and the worker's address and path in the tailnet."""
  view: WorkerView
  ssh: str
  setup: str | None
  report: WorkerReport
  tasks: frozenset[str]
  address: str | None
  path: str | None


def worker_state(hostname: str, tasks: frozenset[str]) -> WorkerState:
  """The worker's state now, given the worker tasks queued or running on it."""
  now = utcnow()
  ssh, setup = Tailscale().ssh(hostname), Workers().setup(hostname)
  report = report_of(WorkerReports().latest(hostname), now)
  view = decide_worker(WorkerFacts(ssh, setup, report, tasks, (now - WorkerReports().started).total_seconds()))
  address = Tailscale().address(hostname)
  return WorkerState(view, ssh, setup, report, tasks, address, Tailscale().path(address) if address else None)
