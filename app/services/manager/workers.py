"""Worker observation and each worker's state, with what tells its pages that it changed."""
import logging

from services.db.models import Worker
from services.history import record_worker_event
from services.tailscale import Tailscale
from services.updates import Fact, Updates, UpdateSource
from services.workers.decisions import TASKS
from services.workers.state import Workers, WorkerState, worker_state

logger = logging.getLogger(__name__)

# The statuses between which a worker's observed changes are recorded in its history: what its tasks show is theirs,
# and checking says nothing is known yet.
OBSERVED = ("offline", "not ready", "online")


class WorkersMixin:
  def worker_states(self, hostnames: list[str] | None = None) -> dict[str, WorkerState]:
    """The state of each worker now (every configured one unless named), by hostname."""
    tasks = self._worker_tasks()
    if hostnames is None:
      hostnames = [worker.hostname for worker in Worker.select(Worker.hostname)]
    return {hostname: worker_state(hostname, frozenset(tasks.get(hostname, ()))) for hostname in hostnames}

  def workers_queue_facts(self) -> list[Fact]:
    """The worker tasks queued or running: a worker whose task is queued, starts or ends is shown again."""
    previous = frozenset()

    def changed(current):
      nonlocal previous
      affected = {hostname for hostname, _ in current ^ previous}
      previous = current
      self._workers_changed(affected)

    return [Fact(lambda: frozenset((hostname, name) for hostname, names in self._worker_tasks().items() for name in names),
                 on_change=changed)]

  def workers_state_fact(self) -> Fact:
    """Every worker's state, checked at each observation and report: a worker whose state changed is shown again, and a
    change of its observed status is recorded in its history."""
    previous = {}

    def changed(current):
      nonlocal previous
      affected = {hostname for hostname in current.keys() | previous.keys() if current.get(hostname) != previous.get(hostname)}
      for hostname in affected:
        before, after = previous.get(hostname), current.get(hostname)
        if (before is not None and after is not None and before.view.status != after.view.status
                and {before.view.status, after.view.status} <= set(OBSERVED)):
          record_worker_event(hostname, "connectivity" if "offline" in (before.view.status, after.view.status) else "readiness",
                              "observed", reason={"status": after.view.status, "reason": after.view.reason})
      previous = current
      self._workers_changed(affected)
      # TODO(36 step 4E): request discovery when the set of ready workers changed.

    return Fact(self.worker_states, on_change=changed)

  def worker_reported(self, hostname: str):
    """A report from the worker arrived: its state may have changed."""
    self.worker_state.check()

  def request_worker_observation(self):
    """Queues the look at every worker's SSH server."""
    self.add_task(task=self.observe_workers, key=("observe_workers",), scopes=set(), executor="app", quiet=True)

  async def observe_workers(self):
    """Asks every configured worker's SSH server, then tells the pages of the workers whose state changed. A worker whose
    SSH server stopped answering is set up again when it returns."""
    hostnames = [worker.hostname for worker in Worker.select(Worker.hostname)]
    await Tailscale().observe_workers(hostnames)
    for hostname in hostnames:
      if Tailscale().ssh(hostname) == "unreachable":
        Workers().record_setup(hostname, None)
    self.worker_state.check()
    # TODO(36 step 4D-2): request the convergence of each reachable worker not set up since the Manager started.

  def request_worker_paths(self):
    """Queues the look at how each reachable worker is reached."""
    self.add_task(task=self.refresh_worker_paths, key=("refresh_worker_paths",), scopes=set(), executor="app", quiet=True)

  async def refresh_worker_paths(self):
    """Finds how each reachable worker is reached, direct or through a relay."""
    hostnames = [worker.hostname for worker in Worker.select(Worker.hostname)]
    await Tailscale().refresh_paths([Tailscale().address(hostname) for hostname in hostnames
                                     if Tailscale().ssh(hostname) == "reachable" and Tailscale().address(hostname)])
    self.worker_state.check()

  def _worker_tasks(self) -> dict[str, set[str]]:
    """The worker tasks queued or running on each worker, by hostname."""
    snapshot = self.task_snapshot()
    tasks = {}
    for task in (*snapshot["running"], *snapshot["queued"]):
      if task["name"] in TASKS and "hostname" in task["params"]:
        tasks.setdefault(task["params"]["hostname"], set()).add(task["name"])
    return tasks

  def _workers_changed(self, hostnames):
    if hostnames:
      Updates().changed(UpdateSource.WORKER_LIST)
    for hostname in hostnames:
      Updates().changed(UpdateSource.WORKER_ENTRY, hostname)
