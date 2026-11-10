"""Each worker's state as its pages show it, worked out from the facts its owners hold, and the setup outcomes the
Manager's convergence leaves."""
from dataclasses import dataclass

from services.base import Base
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from services.workers.decisions import WorkerFacts, WorkerReport, WorkerView, decide_worker, report_of
from utils.common import utcnow


class Workers(Base):
  """Each worker's setup outcome since the Manager started: succeeded, failed or refused (another Sage owns it); None
  until a setup has run, and again once its SSH server stops answering, since it is set up again when it returns."""

  def __init__(self):
    super().__init__()
    self._setups: dict[str, str] = {}

  def setup(self, hostname: str) -> str | None:
    with self.lock:
      return self._setups.get(hostname)

  def record_setup(self, hostname: str, outcome: str | None):
    with self.lock:
      if outcome is None:
        self._setups.pop(hostname, None)
      else:
        self._setups[hostname] = outcome


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
