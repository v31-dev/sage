"""Worker observation and each worker's state, with what tells its pages that it changed; the convergence of a
worker's stack with Sage and its repair."""
import logging
import re
from time import monotonic

import config
from services.cloudflare import Cloudflare
from services.db.models import Instance, Worker
from services.history import record_worker_event
from services.tailscale import Tailscale
from services.updates import Fact, Updates, UpdateSource
from services.worker_reports import WorkerReports
from services.workers.decisions import TASKS, worker_claim_refusal
from services.workers.rendering import InfraInputs, infra_fingerprint, render_infrastructure
from services.workers.state import Workers, WorkerState, worker_state
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed

logger = logging.getLogger(__name__)

# The statuses between which a worker's observed changes are recorded in its history: what its tasks show is theirs,
# and checking says nothing is known yet.
OBSERVED = ("offline", "not ready", "online")
# Why a worker set up but not ready gets its stack started: part of it is down, Traefik does not answer, or it is
# silent (for longer than WORKER_STACK_SILENCE_SECONDS).
REPAIRED = ("stack_down", "traefik_down", "not_reporting")


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
    """Asks every configured worker's SSH server, tells the pages of the workers whose state changed, then asks for the
    convergence of each reachable worker not set up since the Manager started or since it came back, whose address
    changed, or whose last setup failed or was refused WORKER_SETUP_RETRY_SECONDS ago; and for the repair of the stack of
    a worker set up but not ready because of it."""
    hostnames = [worker.hostname for worker in Worker.select(Worker.hostname)]
    await Tailscale().observe_workers(hostnames)
    for hostname in hostnames:
      if Tailscale().ssh(hostname) == "unreachable":
        Workers().record_setup(hostname, None)
    self.worker_state.check()
    now = monotonic()
    for hostname, state in self.worker_state.last.items():
      if state.ssh != "reachable" or state.address is None:
        continue
      failed_at = self.setup_failures.get(hostname)
      if (state.setup is None or Workers().setup_address(hostname) != state.address
              or (state.setup != "succeeded" and (failed_at is None or now - failed_at >= config.WORKER_SETUP_RETRY_SECONDS))):
        self.request_convergence(hostname)
      elif (state.setup == "succeeded" and state.view.reason in REPAIRED
            and (state.view.reason != "not_reporting" or WorkerReports().silent_for(hostname) > config.WORKER_STACK_SILENCE_SECONDS)
            and (hostname not in self.stack_repairs
                 or now - self.stack_repairs[hostname] >= config.WORKER_STACK_REPAIR_COOLDOWN_SECONDS)):
        self.stack_repairs[hostname] = now
        self.request_stack_repair(hostname)
    for hostname in self.setup_failures.keys() - set(hostnames):
      self.setup_failures.pop(hostname, None)

  def request_convergence(self, hostname: str, force: bool = False):
    """Queues the convergence of the worker's stack with Sage; with `force`, its files are written and its stack
    restarted whatever its infra stamp says, as a visible task of its own."""
    self.add_task(task=self.converge_worker, key=("converge_worker", hostname, *(("force",) if force else ())),
                  scopes={f"worker:{hostname}"}, executor="app", params={"hostname": hostname, "force": force},
                  labels={"Worker": hostname}, quiet=not force)

  async def converge_worker(self, hostname: str, force: bool = False):
    """Brings the worker's stack in line with Sage: claims it, makes its tunnel and its `*.<subdomain>.int` record, and
    writes the stack's files and starts it when its infra stamp differs (or on force), else starts what is stopped.
    Another Sage's worker is refused and left alone."""
    worker = Worker.get_or_none(Worker.hostname == hostname)
    address = Tailscale().address(hostname)
    if worker is None or address is None or Tailscale().ssh(hostname) != "reachable":
      logger.info(f"Worker {hostname} is not there to set up; nothing to do.")
      return
    home, cloudflare = config.WORKER_HOME_DIR, Cloudflare()
    try:
      revisions = await Workers().read_revisions(hostname)
      instance_id = Instance.get().id
      refusal = worker_claim_refusal(revisions, instance_id)
      if refusal is not None:
        if Workers().setup(hostname) != "refused":
          record_worker_event(hostname, "ownership", "rejected", reason={"refusal": refusal})
        logger.warning(f"Worker {hostname} is not set up: {refusal}.")
        Workers().record_setup(hostname, "refused", address)
        self.setup_failures[hostname] = monotonic()
        return
      await run_in_executor_with_context(cloudflare.connect)
      if revisions.get("owner") != f"{instance_id} {cloudflare.domain}":
        await Workers().write_revision(hostname, "owner", f"{instance_id} {cloudflare.domain}")
      tunnel = await run_in_executor_with_context(cloudflare.create_tunnel, cloudflare.worker_tunnel_name(hostname))
      await run_in_executor_with_context(cloudflare.sync_worker_records, hostname, worker.subdomain, address)
      inputs = InfraInputs(hostname, address, Tailscale().ip(), cloudflare.domain, cloudflare.tunnel_id, tunnel, config.VERSION,
                           config.REPORT_INTERVAL_SECONDS)
      fingerprint = infra_fingerprint(inputs)
      if force or revisions.get("infra") != fingerprint:
        record_worker_event(hostname, "infrastructure", "admitted", reason={"force": force})
        tunnel_token = (await run_in_executor_with_context(cloudflare.get_tunnel_token, tunnel)).token
        discovery_token = (await run_in_executor_with_context(cloudflare.get_tunnel_token)).token
        _, group = await Tailscale().exec_command(hostname, "stat -c %g /var/run/docker.sock", timeout=30)
        for file in render_infrastructure(inputs, tunnel_token, discovery_token, group[-1].strip(), home):
          await Tailscale().sync_file(hostname, config.TEMPLATES_DIR / "worker/file", f"{home}/{file.path}",
                                      {"CONTENT": file.content}, mode=file.mode)
        compose = f"docker compose -f {home}/docker-compose.yml"
        await Tailscale().exec_command(
            hostname, f"{compose} up -d --wait --remove-orphans --quiet-pull --quiet-build {' '.join(config.WORKER_STACK_SERVICES)}",
            timeout=300)
        # Each reads its configuration only when it starts.
        for service in ("traefik", "vector", "telegraf"):
          await Tailscale().exec_command(hostname, f"{compose} restart {service}", timeout=60)
        await Workers().write_revision(hostname, "infra", fingerprint)
        record_worker_event(hostname, "infrastructure", "succeeded")
      else:
        await self._start_stack(hostname)
      # TODO(36 step 4F): distribute the worker's certificates and request a renewal when one is not valid.
      # TODO(36 step 4E): request discovery, which the worker answers once it is ready.
      Workers().record_setup(hostname, "succeeded", address)
      self.setup_failures.pop(hostname, None)
    except Exception as error:
      Workers().record_setup(hostname, "failed", address)
      self.setup_failures[hostname] = monotonic()
      record_worker_event(hostname, "infrastructure", "failed", reason={"error": str(error)})
      logger.error(f"Worker {hostname} could not be set up: {error}")
      # TODO(36 step 4H): self.notify(f"Failed to set up worker {hostname}: {error}", "error")
      raise TaskFailed()
    finally:
      self.worker_state.check()

  def request_stack_repair(self, hostname: str):
    """Queues the start of whatever of the worker's stack is stopped. It holds the stack's own scope, so a deploy on the
    worker does not delay it; convergence and reboot, holding the whole worker, exclude it."""
    self.add_task(task=self.repair_worker_stack, key=("repair_worker_stack", hostname), scopes={f"worker:{hostname}:stack"},
                  executor="app", params={"hostname": hostname}, labels={"Worker": hostname}, quiet=True)

  async def repair_worker_stack(self, hostname: str):
    if Worker.get_or_none(Worker.hostname == hostname) is None:
      return
    if await self._start_stack(hostname):
      record_worker_event(hostname, "repair", "succeeded", reason={"started": "stack"})
      # TODO(36 step 4H): self.notify(f"Worker {hostname}: started its stack.", "warning")
    else:
      logger.warning(f"Worker {hostname}'s stack was not ready, but starting it changed nothing.")

  async def _start_stack(self, hostname: str) -> bool:
    """`docker compose up -d` for the worker's stack: it starts what is stopped and leaves what runs alone. True when it
    started something."""
    _, lines = await Tailscale().exec_command(
        hostname, f"docker compose -f {config.WORKER_HOME_DIR}/docker-compose.yml up -d --quiet-pull "
        f"{' '.join(config.WORKER_STACK_SERVICES)}", timeout=120)
    return any(re.search(r"\b(Started|Starting|Created)\b", line) for line in lines)

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
