"""Worker observation and each worker's state, with what tells its pages that it changed; the convergence of a
worker's stack with Sage and its repair; and adding, removing and rebooting a worker."""
import asyncio
import logging
import re
from time import monotonic

from peewee import JOIN

import config
from services.certs import TEMPLATE_DIGEST, Certs
from services.cloudflare import Cloudflare
from services.containers.placement import runtime_owner
from services.db.models import Application, Container, Domain, Instance, Project, Worker
from services.domains.config import parse_route_listing, routes_command
from services.domains.updates import domain_changed
from services.history import record_worker_event
from services.metrics import Metrics
from services.notification import Notifications
from services.tailscale import Tailscale
from services.updates import Fact, Updates, UpdateSource
from services.worker_reports import WorkerReports
from services.workers import commands
from services.workers.decisions import TASKS, reboot_outcome, worker_claim_refusal, worker_owner
from services.workers.rendering import InfraInputs, infra_fingerprint, render_infrastructure
from services.workers.state import Workers, WorkerState, worker_payload, worker_state
from utils.common import get_env, utcnow
from utils.errors import Conflict
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed
from utils.queue import OnConflict

from .domains import capture
from .lifecycle import mark

logger = logging.getLogger(__name__)

# The statuses between which a worker's observed changes are recorded in its history: what its tasks show is theirs,
# and checking says nothing is known yet.
OBSERVED = ("offline", "not ready", "online")
# Why a worker set up but not ready gets its stack started: part of it is down, Traefik does not answer, or it is
# silent (for longer than WORKER_STACK_SILENCE_SECONDS).
REPAIRED = ("stack_down", "traefik_down", "not_reporting")
# Why a reboot was not confirmed in time, by how far it got.
REBOOT_FAILURES = {"old_boot": "it did not restart", "not_ready": "it restarted but is not ready",
                   "unknown": "it did not answer"}


class WorkersMixin:
  def worker_states(self, hostnames: list[str] | None = None) -> dict[str, WorkerState]:
    """The state of each worker now (every configured one unless named), by hostname."""
    tasks = self._worker_tasks()
    workers = Worker.select(Worker.hostname, Worker.subdomain)
    if hostnames is not None:
      workers = workers.where(Worker.hostname.in_(hostnames))
    return {worker.hostname: worker_state(worker.hostname, worker.subdomain, frozenset(tasks.get(worker.hostname, ())))
            for worker in workers}

  def worker_list(self) -> list[dict]:
    """Every configured worker's payload, by hostname."""
    workers = list(Worker.select().order_by(Worker.hostname))
    states = self.worker_states([worker.hostname for worker in workers])
    return [worker_payload(worker, states[worker.hostname], Cloudflare().worker_tunnel_name(worker.hostname),
                           Metrics().newest(worker.hostname)) for worker in workers]

  def worker_page(self, hostname: str) -> dict:
    """The worker's payload with its containers' payloads, by application, and why it can't be removed now (None when
    it can); a LookupError when it is not configured."""
    worker = Worker.get_or_none(Worker.hostname == hostname)
    if worker is None:
      raise LookupError("Worker not found.")
    containers = (Container.select(Container, Application, Project, Worker).join(Application).join(Project)
                  .switch(Container).join(Worker).where(Container.worker == hostname).order_by(Project.name, Application.name))
    return {**worker_payload(worker, self.worker_states([hostname])[hostname], Cloudflare().worker_tunnel_name(hostname),
                             Metrics().newest(hostname)),
            "containers": self.container_payloads(containers), "removal_refusal": commands.removal_refusal(worker)}

  def worker_graphs(self, hostname: str, period: str) -> dict:
    """The graphs of a worker, or of the manager itself, over `period`: the host's series, each container's series with
    the name people know it by (a placement's application, a stack service's own name), and the host's size and
    address. A LookupError for any other host."""
    if hostname == get_env("TS_NAME"):
      ip = Tailscale().ip()
    elif Worker.get_or_none(Worker.hostname == hostname) is not None:
      ip = Tailscale().address(hostname)
    else:
      raise LookupError("Worker not found.")
    graphs = Metrics().query_period(hostname, period)
    names = [points[0]["name"] for points in graphs["containers"]]
    labels = {container.id: f"{container.application.project.label} / {container.application.label}"
              for container in Container.select(Container, Application, Project).join(Application).join(Project)
              .where(Container.id.in_({runtime_owner(name) for name in names} - {None}))}
    return {"host": graphs["host"], "meta": {**graphs["meta"], "ip": ip},
            "containers": [{"name": name, "label": labels.get(runtime_owner(name), name), "points": points}
                           for name, points in zip(names, graphs["containers"])]}

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
    """Every worker's state, checked at each observation and report: a worker whose state changed is shown again, with
    the domains routed to it when its readiness changed, a change of its observed status is recorded in its history (with
    a notice when it went offline), a notice is sent when one that was offline answers again, and discovery is published
    again when the ready workers, or those reachable and set up, changed."""
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
          if after.view.status == "offline":
            Notifications().notify(f"Worker {hostname} is offline.", "error", f"/workers/{hostname}")
        if before is not None and after is not None and before.view.status == "offline" and after.ssh == "reachable":
          Notifications().notify(f"Worker {hostname} is reachable again.", "success", f"/workers/{hostname}")
      readiness = {hostname for hostname in affected if hostname in previous and hostname in current
                   and previous[hostname].view.ready != current[hostname].view.ready}
      published = [({hostname for hostname, state in states.items() if state.view.ready},
                    {(hostname, state.address) for hostname, state in states.items()
                     if state.ssh == "reachable" and state.setup == "succeeded"}) for states in (previous, current)]
      previous = current
      self._workers_changed(affected)
      for domain in Domain.select().join(Container, JOIN.LEFT_OUTER).where(
              Container.worker.in_(readiness) | Domain.namespace_worker.in_(readiness)):
        domain_changed(domain)
      if published[0] != published[1]:
        self.request_discovery()

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

  def request_convergence(self, hostname: str, force: bool = False, changed: bool = False) -> str:
    """Queues the convergence of the worker's stack with Sage; with `force` (the worker's resync), as a visible task
    of its own, its files are written and its stack restarted whatever its infra stamp says, and everything else Sage
    keeps on it is written again: its certificates, its Domains' routes and its discovery files. With `changed` (the
    worker's configuration changed), it runs after a convergence already running, which read the configuration before.
    The task's id."""
    return self.add_task(task=self.converge_worker, key=("converge_worker", hostname, *(("force",) if force else ())),
                         scopes={f"worker:{hostname}"}, executor="app", params={"hostname": hostname, "force": force},
                         labels={"Worker": hostname}, on_conflict=OnConflict.REPLACE if changed else OnConflict.DEDUP,
                         quiet=not force).task_id

  async def converge_worker(self, hostname: str, force: bool = False):
    """Brings the worker's stack in line with Sage: claims it, removes the routing files there of Domains gone or bound
    to another worker, makes its tunnel and its `*.<subdomain>.int` record, and writes the stack's files and starts it
    when its infra stamp differs (or on force), else starts what is stopped. It asks for the certificates' renewal when
    its own or the shared discovery certificate does not cover its names, else for their copy to it when it did not
    apply the current ones (its `certs` stamp), and once it is set up for the pass over the Domains' artifacts;
    forced, for its certificates, the routes of the Domains routed to it and its discovery files to be written again
    whatever their stamps say. Another Sage's worker is refused and left alone."""
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
      Workers().record_certificates(hostname, Certs.read_proofs(revisions.get("certs")))
      _, lines = await Tailscale().exec_command(hostname, routes_command(f"{home}/traefik/dynamic"), timeout=30)
      await self.remove_obsolete_routes(hostname, parse_route_listing(lines), {domain.id: capture(domain) for domain in Domain.select()})
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
      certificates = Certs().worker_state(hostname, worker.subdomain, Workers().certificates(hostname), TEMPLATE_DIGEST).values()
      if not all(state["covered"] for state in certificates):
        self.request_certificate_renewal()
      elif not force and not all(state["synced"] for state in certificates):
        self.request_certificate_distribution(hostname)
      Workers().record_setup(hostname, "succeeded", address)
      self.setup_failures.pop(hostname, None)
      self.request_domain_reconciliation()
      if force:
        self.request_certificate_distribution(hostname, force=True)
        self.request_domain_applies(domain.id for domain in Domain.select(Domain.id).join(Container, JOIN.LEFT_OUTER).where(
            (Container.worker == hostname) | (Domain.namespace_worker == hostname)))
        self.request_discovery(hostname, force=True)
    except Exception as error:
      Workers().record_setup(hostname, "failed", address)
      self.setup_failures[hostname] = monotonic()
      record_worker_event(hostname, "infrastructure", "failed", reason={"error": str(error)})
      Notifications().notify(f"Worker {hostname} could not be set up: {error}", "error", f"/workers/{hostname}")
      raise TaskFailed()
    finally:
      self.worker_state.check()

  def request_stack_repair(self, hostname: str):
    """Queues the start of whatever of the worker's stack is stopped. It holds the stack's own scope, so a deploy on the
    worker does not delay it; convergence and reboot, holding the whole worker, exclude it."""
    self.add_task(task=self.repair_worker_stack, key=("repair_worker_stack", hostname), scopes={f"worker:{hostname}:stack"},
                  executor="app", params={"hostname": hostname}, labels={"Worker": hostname}, quiet=True)

  async def repair_worker_stack(self, hostname: str):
    if (Worker.get_or_none(Worker.hostname == hostname) is None or Tailscale().ssh(hostname) != "reachable"
            or Workers().setup(hostname) != "succeeded"):
      logger.info(f"Worker {hostname} is not there to repair; nothing to do.")
      return
    if await self._start_stack(hostname):
      record_worker_event(hostname, "repair", "succeeded", reason={"started": "stack"})
      Notifications().notify(f"Worker {hostname}: started the parts of its stack that were stopped.", "warning",
                             f"/workers/{hostname}")
    else:
      logger.warning(f"Worker {hostname}'s stack was not ready, but starting it changed nothing.")

  def request_add_worker(self, host: str | None, subdomain: str | None) -> tuple[str, str]:
    """Adds the machine of the tailnet with that hostname (any case) or Tailscale IP as a worker, named by its hostname
    in lowercase, with its subdomain: its row now, then a queued add that sets it up. Its hostname and the task's id."""
    host = str(host or "").strip()
    peer = Tailscale().find_peer(host) if host else None
    if peer is None:
      raise LookupError(f"No machine with hostname or IP '{host}' in the tailnet.")
    if peer.hostname == get_env("TS_NAME").lower():
      raise Conflict("A worker with the manager's name is not allowed.")
    commands.add_worker(peer.hostname, subdomain)
    return peer.hostname, self.add_task(task=self.add_worker, key=("add_worker", peer.hostname),
                                        scopes={f"worker:{peer.hostname}"}, executor="app",
                                        params={"hostname": peer.hostname}, labels={"Worker": peer.hostname}).task_id

  async def add_worker(self, hostname: str):
    """Sets a worker just added up: its first convergence is part of the add. A worker whose first setup fails stays,
    and is set up again as any other is."""
    if Worker.get_or_none(Worker.hostname == hostname) is None:
      logger.info(f"Worker {hostname} was removed before it was set up; nothing to do.")
      return
    record_worker_event(hostname, "add", "admitted")
    await Tailscale().observe_worker(hostname)
    try:
      await self.converge_worker(hostname)
    except TaskFailed:
      record_worker_event(hostname, "add", "failed", reason={"setup": "failed"})
      raise
    if Workers().setup(hostname) != "succeeded":
      record_worker_event(hostname, "add", "failed", reason={"setup": Workers().setup(hostname) or "not reached"})
      Notifications().notify(f"Worker {hostname} is added but not set up: "
                             f"{'it is managed by another Sage' if Workers().setup(hostname) == 'refused' else 'its SSH server does not answer'}.",
                             "error", f"/workers/{hostname}")
      raise TaskFailed()
    record_worker_event(hostname, "add", "succeeded")
    Notifications().notify(f"Worker {hostname} added.", "success", f"/workers/{hostname}")

  def request_remove_worker(self, hostname: str) -> str:
    """Queues the worker's removal; refused while it has containers or internal domains. The task's id."""
    worker = Worker.get_or_none(Worker.hostname == hostname)
    if worker is None:
      raise LookupError("Worker not found.")
    refusal = commands.removal_refusal(worker)
    if refusal is not None:
      raise Conflict(refusal)
    return self.add_task(task=self.remove_worker, key=("remove_worker", hostname), scopes={f"worker:{hostname}"},
                         executor="app", params={"hostname": hostname}, labels={"Worker": hostname}).task_id

  async def remove_worker(self, hostname: str):
    """Removes the worker from Sage: its row, then, when it answers and is this Sage's, its stack and Sage's files on
    it; its DNS records and its tunnel; and what the sources hold of it. One that does not answer is removed from Sage
    only, and the VM has to be cleaned up by hand."""
    if Worker.get_or_none(Worker.hostname == hostname) is None:
      logger.info(f"Worker {hostname} is already removed; nothing to do.")
      return
    try:
      commands.remove_worker(hostname)
    except Conflict as refusal:
      record_worker_event(hostname, "remove", "rejected", reason={"refusal": str(refusal)})
      Notifications().notify(f"Worker {hostname} was not removed: {refusal}", "error", f"/workers/{hostname}")
      raise TaskFailed()
    record_worker_event(hostname, "remove", "admitted")
    home, cleaned, left = config.WORKER_HOME_DIR, False, []
    if await Tailscale().reachable(hostname):
      try:
        owner = worker_owner(await Workers().read_revisions(hostname))
        if owner is not None and owner[0] == Instance.get().id:
          await Tailscale().exec_command(
              hostname, f"docker compose -f {home}/docker-compose.yml down --remove-orphans && rm -rf {home}", timeout=120)
          cleaned = True
      except Exception as error:
        left.append(f"its stack could not be stopped ({error}): stop it and remove its files by hand")
    else:
      left.append("it does not answer: anything on the VM, including the VM itself, has to be removed by hand")
    cloudflare = Cloudflare()
    await run_in_executor_with_context(cloudflare.connect)
    await run_in_executor_with_context(cloudflare.sync_worker_records, hostname)
    try:
      await run_in_executor_with_context(cloudflare.delete_tunnel, cloudflare.worker_tunnel_name(hostname))
    except Exception as error:
      left.append(f"its Cloudflare tunnel could not be deleted ({error}): delete it by hand")
    Workers().forget(hostname)
    WorkerReports().forget(hostname)
    Metrics().forget(hostname)
    await Tailscale().forget(hostname)
    self.setup_failures.pop(hostname, None)
    self.stack_repairs.pop(hostname, None)
    record_worker_event(hostname, "remove", "succeeded", reason={"cleaned": cleaned})
    self.request_certificate_renewal()
    if left:
      Notifications().notify(f"Worker {hostname} is removed from Sage, but " + "; ".join(left) + ".", "warning", "/workers")
    else:
      Notifications().notify(f"Worker {hostname} removed.", "success", "/workers")
    self.worker_state.check()

  def request_reboot(self, hostname: str) -> str:
    """Queues the worker's reboot. The task's id."""
    if Worker.get_or_none(Worker.hostname == hostname) is None:
      raise LookupError("Worker not found.")
    return self.add_task(task=self.reboot_worker, key=("reboot_worker", hostname), scopes={f"worker:{hostname}"},
                         executor="app", params={"hostname": hostname}, labels={"Worker": hostname}).task_id

  async def reboot_worker(self, hostname: str):
    """Reboots the worker and waits, holding it, until a new boot of it is set up again and ready on a report that arrived
    after that boot was seen: verified, or failed once WORKER_REBOOT_LIMIT_SECONDS have passed, saying how far it got
    (old_boot, not_ready, unknown). A later return is ordinary observation."""
    worker = Worker.get_or_none(Worker.hostname == hostname)
    if worker is None:
      return
    if Tailscale().ssh(hostname) != "reachable" or Workers().setup(hostname) != "succeeded":
      record_worker_event(hostname, "reboot", "rejected", reason={"ssh": Tailscale().ssh(hostname), "setup": Workers().setup(hostname)})
      Notifications().notify(f"Worker {hostname} was not rebooted: it is not reachable and set up.", "error",
                             f"/workers/{hostname}")
      raise TaskFailed()
    baseline = await self._boot_id(hostname)
    record_worker_event(hostname, "reboot", "admitted", reason={"boot_id": baseline})
    for container in worker.containers:
      await mark(container, "Worker rebooted by Sage")
    await Tailscale().exec_command(hostname, "(sleep 2 && reboot) </dev/null >/dev/null 2>&1 &", timeout=30)
    Workers().record_setup(hostname, None)
    deadline, outcome, booted_at = monotonic() + config.WORKER_REBOOT_LIMIT_SECONDS, "unknown", None
    while monotonic() < deadline:
      await asyncio.sleep(config.WORKER_REBOOT_POLL_SECONDS)
      try:
        boot = await self._boot_id(hostname)
      except Exception:
        boot = None
      if boot is not None and boot != baseline:
        booted_at = booted_at or utcnow()
        if Workers().setup(hostname) != "succeeded":
          await Tailscale().observe_worker(hostname)
          try:
            await self.converge_worker(hostname)
          except TaskFailed:
            pass
      received = WorkerReports().latest(hostname)
      reported = booted_at is not None and received is not None and received.arrived_at >= booted_at
      outcome = reboot_outcome(baseline, boot, worker_state(hostname, worker.subdomain, frozenset()).view.ready and reported)
      if outcome == "verified":
        break
    if outcome != "verified":
      record_worker_event(hostname, "reboot", "failed", reason={"outcome": outcome})
      Notifications().notify(f"Worker {hostname}'s reboot could not be confirmed in time: {REBOOT_FAILURES[outcome]}.", "error",
                             f"/workers/{hostname}")
      raise TaskFailed()
    record_worker_event(hostname, "reboot", "verified")
    Notifications().notify(f"Worker {hostname} rebooted.", "success", f"/workers/{hostname}")

  def worker_subdomain_changed(self, hostname: str):
    """After the worker's subdomain changed (its command): its `*.<subdomain>.int` record moves with its convergence,
    its internal and TCP domains, whose names are under it, are applied again, and its certificate is renewed for the
    new names."""
    self.request_convergence(hostname, changed=True)
    self.request_domain_applies(domain.id for domain in Domain.select(Domain.id).where(Domain.namespace_worker == hostname))
    self.request_certificate_renewal()

  async def _boot_id(self, hostname: str) -> str:
    _, lines = await Tailscale().exec_command(hostname, "cat /proc/sys/kernel/random/boot_id", timeout=10)
    return lines[0].strip()

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
