import asyncio
import logging
import re
from dataclasses import replace
from typing import Callable

import config
from services.db import Application, Container, Instance, Worker
from services.metrics import Metrics
from services.updates import Fact, Updates, UpdateSource
from services.worker_reports import WorkerReports
from utils.common import utcnow
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed

from ._common import WORKER_INFRA_TEMPLATES, app_dir, content_hash, routing_input_hash
from .decisions import (WORKER_TASKS, WorkerInputs, WorkerReboot, WorkerRow, plan_worker_sync, worker_claim_refusal, worker_entry, worker_owner,
                        worker_stack_problem, worker_with_row, worker_without_row)

logger = logging.getLogger(__name__)


class WorkersMixin:
  async def sync_workers(self, force: bool = False, check_stamps: bool = False):
    """
    Check the workers for changes. Workers are added by hand (`add_worker`), so a machine of
    the tailnet that is not a known worker is not looked at. `check_stamps` also sets up
    workers that stayed online, letting their stamps decide what to redo (at startup, since
    the env may have changed while Sage was stopped). Whether a worker is online is whether its
    SSH server answers (`Tailscale.online_states`), not Tailscale's own flag. The decisions are
    `plan_worker_sync`'s:
    - Offline workers (not answering, or not in the tailnet)
    - Updated workers (IPs have changed)
    - Recently online workers (previously offline but now answering)
    After that, a worker whose reports say its stack is down gets it started.
    """
    known = [WorkerRow(w.hostname, w.ip, self.is_worker_online(w.hostname)) for w in Worker.select()]
    names = {row.hostname for row in known}
    listed = [peer for peer in await run_in_executor_with_context(self.tailscale.peers) if peer.hostname in names]
    was_online = {row.hostname: row.online for row in known}
    online = await self.tailscale.online_states({w.hostname: was_online.get(w.hostname, False) for w in listed})
    peers = [WorkerRow(w.hostname, w.ip, online[w.hostname]) for w in listed]

    logger.info(f"Existing workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in known]}")
    logger.info(f"Tailscale workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in peers]}")

    for action in plan_worker_sync(known, peers, force, check_stamps):
      hostname = action.worker.hostname
      try:
        if action.kind == "offline":
          logger.info(f"Worker {hostname}: {action.reason}.")
          await run_in_executor_with_context(self.set_worker_offline, action.worker)
        else:
          logger.info(f"{'Force syncing' if action.force else 'Syncing'} worker {hostname}: {action.reason}.")
          await self.setup_worker(action.worker, force=action.force)
      except Exception as e:
        logger.error(f"sync_workers failed for {hostname}: {e}")

    await self.repair_worker_stacks()
    self.update_workers({})  # works the list out as of now, for what only the clock changed

  async def refresh_worker_paths(self):
    """Find how each online worker is reached, direct or through a relay (`Tailscale.path`), and give it to the
    worker list."""
    await self.tailscale.refresh_paths([worker.ip for worker in self.online_workers()])
    self.update_workers({worker["hostname"]: {"path": self.tailscale.path(worker["ip"])} for worker in self.worker_list})

  def worker_tasks_busy(self) -> tuple:
    """The workers that are being added, removed or rebooted, with the task."""
    snapshot = self.task_snapshot()
    busy = (task for task in (*snapshot["running"], *snapshot["queued"]) if task["name"] in WORKER_TASKS)
    return tuple(sorted((task["name"], task["params"]["worker_hostname"]) for task in busy))

  def workers_queue_facts(self) -> list[Fact]:
    """The worker tasks are given to the worker list when the set of them differs; they fire no source of their own."""
    return [Fact(self.worker_tasks_busy, on_change=self._set_worker_tasks)]

  def is_worker_online(self, hostname: str) -> bool:
    """Whether the worker answered the last sync and was set up (`online` in `worker_list`). Not known, so not
    online, until a sync has looked at it."""
    return any(worker["online"] for worker in self.worker_list if worker["hostname"] == hostname)

  def online_workers(self) -> list[Worker]:
    online = {worker["hostname"] for worker in self.worker_list if worker["online"]}
    return [worker for worker in Worker.select() if worker.hostname in online]

  def worker_entry(self, hostname: str) -> dict | None:
    """The worker's entry in `worker_list`, None for a name that is not in it."""
    return next((worker for worker in self.worker_list if worker["hostname"] == hostname), None)

  def worker_status(self, hostname: str) -> str:
    """What the worker is shown as (`worker_status` in `decisions`), as of the last change of the worker list.
    "offline" for a name that is not in it."""
    entry = self.worker_entry(hostname)
    return entry["status"] if entry else "offline"

  def worker_counts(self) -> dict:
    """How many workers there are and how many are online, from `worker_list`: a worker counts as online when its
    status is "online", so the count and the statuses agree."""
    return {"total": len(self.worker_list), "online": sum(worker["status"] == "online" for worker in self.worker_list)}

  def update_workers(self, changes: dict[str, dict]):
    """Give the worker list the inputs its owners push: `changes` maps a hostname to the `WorkerInputs` fields that
    changed, and the rest of its record stays. A record is made for a name that has none, a worker only once the
    row writer has given it an `ip`."""
    def edit(records: dict):
      for hostname, inputs in changes.items():
        records[hostname] = replace(records.get(hostname, WorkerInputs()), **inputs)
    self._edit_workers(edit)

  def update_worker(self, hostname: str, **inputs):
    self.update_workers({hostname: inputs})

  def _set_worker_tasks(self, busy: tuple):
    tasks = {}
    for task, hostname in busy:
      tasks.setdefault(hostname, set()).add(task)

    def edit(records: dict):
      for hostname in records.keys() | tasks.keys():
        records[hostname] = replace(records.get(hostname, WorkerInputs()), tasks=frozenset(tasks.get(hostname, ())))
    self._edit_workers(edit)

  def _save_worker(self, hostname: str, ip: str):
    """Write the worker's row and its record together."""
    Worker.insert(hostname=hostname, ip=ip).on_conflict(
        conflict_target=[Worker.hostname], preserve=[Worker.ip]
    ).execute()
    path = self.tailscale.path(ip)

    def edit(records: dict):
      records[hostname] = worker_with_row(records.get(hostname, WorkerInputs()), ip, path)
    self._edit_workers(edit)

  def _forget_worker(self, hostname: str):
    """Delete the worker's row and its record together."""
    Worker.delete().where(Worker.hostname == hostname).execute()

    def edit(records: dict):
      if hostname in records:
        records[hostname] = worker_without_row(records[hostname])
    self._edit_workers(edit)

  def load_workers(self):
    """Read the Worker table into the worker list, at start and after a restore replaced the table: the only
    reads of it, as `_save_worker` and `_forget_worker` are the only writes."""
    rows = {worker.hostname: worker.ip for worker in Worker.select()}
    paths = {hostname: self.tailscale.path(ip) for hostname, ip in rows.items()}

    def edit(records: dict):
      for hostname in records.keys() | rows.keys():
        record = records.get(hostname, WorkerInputs())
        records[hostname] = (worker_with_row(record, rows[hostname], paths[hostname]) if hostname in rows
                             else worker_without_row(record))
    self._edit_workers(edit)

  def _edit_workers(self, edit: Callable[[dict], None]):
    """The one place the worker list is worked out. `edit` changes the records (hostname to `WorkerInputs`) under
    the lock; then each worker's entry is derived from its record and the clock, and the list is replaced. Nothing
    is read from the table, the queue, the reports or Tailscale here: their owners push. Fires `worker_list` when the
    list differs, and `worker_entry` for each worker whose entry does. What only the clock changes (a report going
    stale, the start-up grace, the reboot limit) shows the next time the list is worked out, which every push does."""
    with self._worker_list_lock:
      edit(self._worker_inputs)
      for hostname in [hostname for hostname, record in self._worker_inputs.items() if record == WorkerInputs()]:
        del self._worker_inputs[hostname]
      now, started = utcnow(), WorkerReports().started
      workers = [(hostname, record) for hostname, record in sorted(self._worker_inputs.items()) if record.ip is not None]
      listed = {entry["hostname"]: entry for entry in self.worker_list}
      self.worker_list = [worker_entry(hostname, record, now, started) for hostname, record in workers]
      now_listed = {entry["hostname"]: entry for entry in self.worker_list}
    differing = [hostname for hostname in listed.keys() | now_listed.keys() if listed.get(hostname) != now_listed.get(hostname)]
    if differing:
      Updates().changed(UpdateSource.WORKER_LIST)
    for hostname in differing:
      Updates().changed(UpdateSource.WORKER_ENTRY, hostname)

  async def start_worker_stack(self, worker) -> bool:
    """`docker compose up -d` for the worker's stack: it starts what is stopped and leaves what runs
    alone. True when it started something."""
    _, lines = await self.tailscale.exec_command(
        worker.hostname,
        f"docker compose -f {self.worker_home_dir}/docker-compose.yml up -d --quiet-pull "
        f"{' '.join(config.WORKER_STACK_SERVICES)}",
        timeout=120,
    )
    return any(re.search(r"\b(Started|Starting|Created)\b", line) for line in lines)

  async def repair_worker_stacks(self):
    """Start the stack of every online worker whose reports say part of it is down (`worker_stack_problem`).
    Docker does not retry a container whose start failed, as Traefik's does when the worker boots
    before its Tailscale address exists. A worker is tried at most once per cooldown, and not while it is
    being rebooted or removed."""
    now = utcnow()
    self.worker_stack_repairs = {
        hostname: at for hostname, at in self.worker_stack_repairs.items()
        if (now - at).total_seconds() < config.WORKER_STACK_REPAIR_COOLDOWN_SECONDS}
    reports = WorkerReports()
    for worker in self.online_workers():
      current = reports.current(worker.hostname)
      problem = worker_stack_problem(current.snapshot if current else None, reports.silent_for(worker.hostname))
      if (problem is None or worker.hostname in self.worker_stack_repairs
              or self.worker_status(worker.hostname) in ("rebooting", "removing")):
        continue
      self.worker_stack_repairs[worker.hostname] = now
      try:
        started = await self.start_worker_stack(worker)
      except Exception as e:
        self.notify(f"Worker {worker.hostname}: {problem}; starting its stack failed: {e}", "error")
        continue
      if started:
        self.notify(f"Worker {worker.hostname}: {problem}; started its stack.", "warning")
      else:
        logger.warning(f"Worker {worker.hostname}: {problem}, but starting its stack changed nothing.")

  def request_stale_application_syncs(self, domain: str, revisions: dict) -> list[str]:
    """Request a Traefik sync of each application whose routing stamp in `revisions` (a worker's) is not what its files
    would be now, which covers hosted and mesh files alike. Returns the names of those."""
    stale = []
    for application in Application.select():
      app_hash = routing_input_hash(
          domain, list(application.domains), list(application.containers), self.is_worker_online)
      if revisions.get(application.qualified_name) != app_hash:
        self.request_application_traefik_sync(application)
        stale.append(application.qualified_name)
    return stale

  def infra_hash(self) -> str:
    """Hash of every input `setup_worker` renders into worker infra files.
    Computed from current truth on demand — the only stored copy is the
    worker-side `revisions/infra` stamp."""
    return content_hash({
        "templates": WORKER_INFRA_TEMPLATES,
        "version": self.version,
        "domain": self.cloudflare.domain,
        "tunnel": self.cloudflare.tunnel_id,
        "manager_ip": self.tailscale.ip(),
        "report_interval": config.REPORT_INTERVAL_SECONDS,
    })

  async def read_worker_revisions(self, hostname: str) -> dict:
    """All revision stamps stored on a worker ({} when none exist yet)."""
    _, lines = await self.tailscale.exec_command(
        hostname,
        f"grep -H . {self.worker_home_dir}/revisions/* 2>/dev/null || true",
    )
    revisions = {}
    for line in lines:
      path, separator, value = line.partition(":")
      if separator and value.strip():
        revisions[path.rsplit("/", 1)[-1]] = value.strip()
    return revisions

  async def write_worker_revision(self, hostname: str, key: str, value: str):
    await self.tailscale.sync_file(
        hostname,
        app_dir / "templates/worker/file",
        f"{self.worker_home_dir}/revisions/{key}",
        {"CONTENT": value},
    )

  async def cleanup_orphan_containers(self, worker):
    """
    Existence-level cleanup during worker convergence: app containers and
    directories with no owning Container row (e.g. force-deleted while this
    worker was offline) would otherwise run forever and squat on a future
    app's container name.
    """
    apps_root = f"{self.worker_home_dir}/applications"

    # The compose working-dir label proves sage created the container, even if
    # its app dir was deleted later; anything without it is not ours to touch.
    _, ps_lines = await self.tailscale.exec_command(
        worker.hostname,
        "docker ps -a --format '{{.Names}}|{{.Label \"com.docker.compose.project.working_dir\"}}'",
    )
    sage_containers = set()
    for line in ps_lines:
      name, _, working_dir = line.partition("|")
      if working_dir.startswith(f"{apps_root}/") and re.fullmatch(r"[a-z0-9-]+", name):
        sage_containers.add(name)

    _, dir_names = await self.tailscale.exec_command(
        worker.hostname, f"ls -1 {apps_root} 2>/dev/null || true")
    sage_dirs = {name for name in dir_names if re.fullmatch(r"[a-z0-9-]+", name)}

    # Read the owning rows LAST: a Container row is created (instant route
    # write) before any deploy materializes its app dir, so a dir/container
    # captured above whose row is being created concurrently is present in this
    # read. Anything left with no row is a genuine orphan, never an in-flight
    # create being reaped mid-deploy.
    expected = {
        container.application.qualified_name
        for container in Container.select().where(Container.worker == worker.hostname)
    }

    orphan_containers = sage_containers - expected
    orphan_dirs = sage_dirs - expected

    for name in sorted(orphan_containers | orphan_dirs):
      # `docker rm -f` only for names the ps scan proved are sage containers
      remove_container = f"docker rm -f {name} 2>/dev/null || true; " if name in orphan_containers else ""
      await self.tailscale.exec_command(
          worker.hostname,
          f'[ ! -f "{apps_root}/{name}/docker-compose.yml" ] || '
          f'docker compose -f "{apps_root}/{name}/docker-compose.yml" down --volumes --rmi all --remove-orphans; '
          + remove_container +
          f"rm -rf {apps_root}/{name}; "
          f"rm -f {self.worker_home_dir}/revisions/{name}",
          timeout=120,
      )
    if orphan_containers or orphan_dirs:
      self.notify(
          f"Removed orphaned containers on worker {worker.hostname}: "
          f"{', '.join(sorted(orphan_containers | orphan_dirs))}.",
          "warning")

  async def add_worker(self, worker_hostname: str):
    """
    Add a machine of the tailnet as a worker: its SSH server must answer and it must not be managed
    by another Sage (`setup_worker` checks). `setup_worker` does the rest.
    """
    peer = await run_in_executor_with_context(self.tailscale.find_peer, worker_hostname)
    if peer is None or not await self.tailscale.reachable(peer.hostname):
      self.notify(f"Failed to add worker {worker_hostname}: it is not in the tailnet or its SSH server does not answer.", "error")
      raise TaskFailed()
    await self.setup_worker(peer)

  def instance_id(self) -> str:
    return Instance.get().id

  async def setup_worker(self, worker, force: bool = False):
    """
    Converge a worker to current manager state — the single idempotent heal
    for new, rejoining, IP-changed, and force-resynced workers. The worker's
    revision stamps decide how much work that is: a matching infra stamp
    skips the file syncs / compose up / restarts, matching cert and per-app
    stamps skip those repairs, and a worker that missed nothing costs one
    read. `force` deletes the stamps first — distrust everything, converge
    from scratch. A worker that another Sage manages (its owner stamp names
    another instance) is refused. On failure, the worker row + DNS are only
    cleaned up if this call was adding a brand-new worker.
    """
    existing = Worker.get_or_none(Worker.hostname == worker.hostname)
    is_new = existing is None
    logger.info(f"{'Adding' if is_new else 'Converging'} worker {worker.hostname}.")
    try:
      # Connected at startup; this retries if that failed, and sets the tunnel the
      # infra stamp includes.
      await run_in_executor_with_context(self.cloudflare.connect)
      domain = self.cloudflare.domain

      revisions = await self.read_worker_revisions(worker.hostname)
      instance_id = self.instance_id()
      refusal = worker_claim_refusal(revisions, instance_id)
      if refusal:
        raise Exception(refusal)
      if force:
        await self.tailscale.exec_command(
            worker.hostname, f"rm -rf {self.worker_home_dir}/revisions")
        revisions = {}
      # Claim it before changing anything, so a failed setup leaves it claimed.
      if revisions.get("owner") != f"{instance_id} {domain}":
        await self.write_worker_revision(worker.hostname, "owner", f"{instance_id} {domain}")
      # The IP is rendered into infra files, so a changed IP is infra drift
      # even under a matching stamp.
      infra_stale = (revisions.get("infra") != self.infra_hash()
                     or is_new or existing.ip != worker.ip)

      self._save_worker(worker.hostname, worker.ip)
      # Always: the offline transition deleted the DNS record.
      await run_in_executor_with_context(
          self.cloudflare.create_dns_record,
          name=f"*.int.{domain}",
          content=worker.ip,
          comment=f"sage-worker-{worker.hostname}",
          type="A",
      )

      if infra_stale:
        tunnel_token = (await run_in_executor_with_context(self.cloudflare.get_tunnel_token)).token
        # Telegraf runs as a user in the Docker socket's group, which differs between hosts.
        _, socket_group = await self.tailscale.exec_command(
            worker.hostname, "stat -c %g /var/run/docker.sock", timeout=30)
        docker_gid = socket_group[-1].strip()

        # Sync files
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/docker-compose.yml",
            f"{self.worker_home_dir}/docker-compose.yml",
        )
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/worker.env",
            f"{self.worker_home_dir}/.env",
            {
                "SAGE_HOME": self.worker_home_dir,
                "TS_IP": worker.ip,
                "TS_HOSTNAME": worker.hostname,
                "TUNNEL_TOKEN": tunnel_token,
                "DOCKER_GID": docker_gid,
            },
            mode=0o600,
        )
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/traefik/traefik.yml",
            f"{self.worker_home_dir}/traefik/traefik.yml",
        )
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/traefik/config.yml",
            f"{self.worker_home_dir}/traefik/dynamic/config.yml",
            {"DOMAIN": domain or "", "HOSTNAME": worker.hostname},
        )
        # File-provider TLS config pointing at the PEM the manager syncs below.
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/traefik/certs.yml",
            f"{self.worker_home_dir}/traefik/dynamic/certs.yml",
        )
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/vector/vector.yml",
            f"{self.worker_home_dir}/vector/config/vector.yml",
            {"IP": self.tailscale.ip(), "HOSTNAME": worker.hostname},
        )
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/telegraf/telegraf.conf",
            f"{self.worker_home_dir}/telegraf/telegraf.conf",
            {
                "IP": self.tailscale.ip(),
                "HOSTNAME": worker.hostname,
                "TS_IP": worker.ip,
                "INTERVAL": config.REPORT_INTERVAL_SECONDS,
            },
        )

      # Land the cert before any container start/restart below
      repaired = []
      if (self.certs.workers.is_valid()
              and revisions.get("certs") != self.certs.workers.hash()):
        await self.sync_certificates_to_worker(worker)
        repaired.append("certificates")

      if infra_stale:
        # Start containers
        await self.tailscale.exec_command(
            worker.hostname,
            f"docker compose -f {self.worker_home_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build "
            f"{' '.join(config.WORKER_STACK_SERVICES)}",
            timeout=300,
        )

        # Restart traefik to pick up static config changes
        await self.tailscale.exec_command(
            worker.hostname,
            f"docker compose -f {self.worker_home_dir}/docker-compose.yml restart traefik",
            timeout=60,
        )

        # Restart vector to pick up config changes
        await self.tailscale.exec_command(
            worker.hostname,
            f"docker compose -f {self.worker_home_dir}/docker-compose.yml restart vector",
            timeout=60,
        )

        # Restart telegraf to pick up config changes
        await self.tailscale.exec_command(
            worker.hostname,
            f"docker compose -f {self.worker_home_dir}/docker-compose.yml restart telegraf",
            timeout=60,
        )
      else:
        # Nothing drifted, but a container may be stopped.
        await self.start_worker_stack(worker)

      self.update_worker(worker.hostname, online=True)

      repaired.extend(self.request_stale_application_syncs(domain, revisions))

      await self.cleanup_orphan_containers(worker)

      if infra_stale:
        # Success commit marker
        await self.write_worker_revision(worker.hostname, "infra", self.infra_hash())
        self.notify(f"Worker {worker.hostname} {'added' if is_new else 'synced'}.")
      elif repaired:
        self.notify(
            f"Worker {worker.hostname} converged with infra unchanged; repaired: {', '.join(repaired)}.")
      else:
        logger.info(f"Worker {worker.hostname} converged with infra unchanged; nothing stale.")
    except Exception as e:
      action = "setup" if is_new else "sync"
      self.notify(f"Failed to {action} worker {worker.hostname} : {e}", "error")
      # Only roll back the worker row + DNS if this was a fresh add.
      if is_new:
        await self.remove_worker(worker.hostname)
      raise Exception(f"Failed to {action} worker {worker.hostname} : {e}")

  async def reboot_worker(self, worker_hostname: str):
    """
    Reboot a worker host over Tailscale SSH. The reboot is backgrounded so the
    SSH command returns before the host network drops.
    """
    worker = Worker.get_or_none(Worker.hostname == worker_hostname)
    if not worker:
      return

    try:
      await self.tailscale.exec_command(
          worker.hostname, "(sleep 2 && reboot) </dev/null >/dev/null 2>&1 &", timeout=30)
    except Exception as e:
      self.notify(f"Failed to reboot worker {worker.hostname}: {e}", "error")
      raise Exception(f"Failed to reboot worker {worker.hostname}: {e}")

    self.update_worker(
        worker.hostname, reboot=WorkerReboot(utcnow(), WorkerReports().started_at(worker.hostname, "telegraf", current=False)))

    # Hold the scope past the backgrounded reboot's delay so a deploy can't grab a
    # still-up worker in the gap between this task finishing and the host dropping.
    await asyncio.sleep(3)

    self.notify(f"Worker {worker.hostname} reboot triggered.")

  async def remove_worker(self, worker_hostname: str):
    """
    Forget a worker. One that answers a ping and is this Sage's is cleaned up first: its stack is
    stopped and Sage's files on it removed, so that another Sage can take it. One that is offline
    (the VM may be gone) is forgotten as it is, and its containers are removed from Sage only. A worker
    that is online and has containers is refused: they have to be deleted first -
    - Remove from Manager database
    - Delete Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare automatically cleans up dead tunnels
    """
    worker = Worker.get_or_none(Worker.hostname == worker_hostname)
    if not worker:
      return

    # Re-check under the scope lock: a deploy could have placed a container here
    # between the route's check and this task running. Fail loudly so it surfaces
    # in the task table rather than vanishing.
    if self.is_worker_online(worker_hostname) and worker.containers.count() > 0:
      logger.error(f"Worker {worker_hostname} gained containers before removal; aborting.")
      raise TaskFailed()

    logger.info(f"Removing worker {worker.hostname} from manager.")
    containers = list(worker.containers)
    for container in containers:
      await self.delete_container(container.id, force=True)

    # The containers of an offline worker were deleted without cleaning up after them, so its files stay.
    if not containers and await self.tailscale.reachable(worker.hostname):
      try:
        owner = worker_owner(await self.read_worker_revisions(worker.hostname))
        if owner and owner[0] == self.instance_id():
          await self.tailscale.exec_command(
              worker.hostname,
              f"docker compose -f {self.worker_home_dir}/docker-compose.yml down --remove-orphans "
              f"&& rm -rf {self.worker_home_dir}",
              timeout=120,
          )
      except Exception as e:
        self.notify(
            f"Worker {worker.hostname} is removed from Sage, but its stack could not be stopped: {e}. "
            "Stop it and remove its files by hand.", "warning")
    else:
      self.notify(
          f"Worker {worker.hostname} was offline, so Sage could not clean it up. It is removed from Sage "
          "only; anything on the VM, including the VM itself, has to be removed by hand.", "warning")
    self._forget_worker(worker.hostname)
    WorkerReports().forget(worker.hostname)
    Metrics().forget(worker.hostname)
    await self.tailscale.forget(worker.hostname)
    await run_in_executor_with_context(
        self.cloudflare.delete_dns_record, name=f"*.int.{self.cloudflare.domain}", content=worker.ip)
    logger.info(f"Worker {worker.hostname} removed from manager.")

  def set_worker_offline(self, worker):
    """
    A worker can be set to offline if it is expected to come back soon, e.g. after a reboot -
    - Mark as offline in Manager database
    - Delete Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare will automatically handle an offline tunnel
    """
    logger.info(f"Setting worker {worker.hostname} offline.")
    self.update_worker(worker.hostname, online=False)
    self.cloudflare.delete_dns_record(name=f"*.int.{self.cloudflare.domain}", content=worker.ip)
    self.notify(f"Worker {worker.hostname} is offline.", "error")
