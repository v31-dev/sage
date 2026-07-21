import logging
import re

from services.db import Application, Container, Worker
from services.settings import Settings
from utils.common import get_env
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed

from ._common import WORKER_INFRA_TEMPLATES, app_dir, content_hash, routing_input_hash

logger = logging.getLogger(__name__)


class WorkersMixin:
  async def sync_workers(self, force: bool = False):
    """
    Check for worker changes.
    - New workers (not in db but in tailscale)
    - Offline workers (in db but tailscale says offline)
    - Updated workers (IPs have changed)
    - Recently online workers (previously offline but now back online)
    - Offline workers (in db but not in tailscale)
    """
    existing_workers = list(Worker.select())
    tailscale_workers = await run_in_executor_with_context(
        self.tailscale.get_by_tag, get_env("WORKER_TAILSCALE_TAG"))

    logger.info(
        f"Existing workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in existing_workers]}")
    logger.info(
        f"Tailscale workers: {[(w.hostname, w.ip, 'online' if w.online else 'offline') for w in tailscale_workers]}")

    for worker in tailscale_workers:
      try:
        existing_worker = next((w for w in existing_workers if w.hostname == worker.hostname), None)
        if existing_worker:
          if existing_worker.online and not worker.online:
            # worker went offline
            logger.info(f"Worker {worker.hostname} went offline.")
            await run_in_executor_with_context(self.set_worker_offline, worker)
          elif not existing_worker.online and worker.online:
            # Worker came back online (or an interrupted setup left it flagged
            # offline). Convergence is stamp-driven: only what drifted during
            # the outage is repaired.
            logger.info(f"Worker {worker.hostname} came back online.")
            await self.setup_worker(worker)
          elif existing_worker.ip != worker.ip:
            # worker IP changed (worker was re-created)
            logger.info(
                f"Worker {
                    worker.hostname} IP changed from {
                    existing_worker.ip} to {
                    worker.ip}.")
            await self.setup_worker(worker)
          elif force:
            # Force re-sync: distrust every stamp and converge from scratch.
            logger.info(f"Force syncing worker {worker.hostname}.")
            await self.setup_worker(worker, force=True)
        else:
          # new worker
          logger.info(f"New worker {worker.hostname} detected.")
          await self.setup_worker(worker)
      except Exception as e:
        logger.error(f"sync_workers failed for {worker.hostname}: {e}")

    for existing_worker in existing_workers:
      try:
        if (not any(w.hostname == existing_worker.hostname for w in tailscale_workers)
                and existing_worker.online):
          # worker went offline or was removed
          logger.info(
              f"Worker {existing_worker.hostname} not found in tailscale. Presumed it went offline.")
          await run_in_executor_with_context(self.set_worker_offline, existing_worker)
      except Exception as e:
        logger.error(f"sync_workers offline-check failed for {existing_worker.hostname}: {e}")

  def infra_hash(self) -> str:
    """Hash of every input `setup_worker` renders into worker infra files.
    Computed from current truth on demand — the only stored copy is the
    worker-side `revisions/infra` stamp."""
    return content_hash({
        "templates": WORKER_INFRA_TEMPLATES,
        "version": self.version,
        "domain": Settings().get("cloudflare", "domain"),
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

  async def setup_worker(self, worker, force: bool = False):
    """
    Converge a worker to current manager state — the single idempotent heal
    for new, rejoining, IP-changed, and force-resynced workers. The worker's
    revision stamps decide how much work that is: a matching infra stamp
    skips the file syncs / compose up / restarts, matching cert and per-app
    stamps skip those repairs, and a worker that missed nothing costs one
    read. `force` deletes the stamps first — distrust everything, converge
    from scratch. On failure, the worker row + DNS are only cleaned up if
    this call was adding a brand-new worker.
    """
    existing = Worker.get_or_none(Worker.hostname == worker.hostname)
    is_new = existing is None
    logger.info(f"{'Adding' if is_new else 'Converging'} worker {worker.hostname}.")
    try:
      domain = Settings().get("cloudflare", "domain")

      if force:
        await self.tailscale.exec_command(
            worker.hostname, f"rm -rf {self.worker_home_dir}/revisions")
      revisions = await self.read_worker_revisions(worker.hostname)
      # The IP is rendered into infra files, so a changed IP is infra drift
      # even under a matching stamp.
      infra_stale = (revisions.get("infra") != self.infra_hash()
                     or is_new or existing.ip != worker.ip)

      Worker.insert(hostname=worker.hostname, ip=worker.ip).on_conflict(
          conflict_target=[Worker.hostname], preserve=[Worker.ip]
      ).execute()
      # Always: the offline transition deleted the DNS record.
      await run_in_executor_with_context(
          self.cloudflare.create_dns_record,
          name=f"*.int.{domain}",
          content=worker.ip,
          comment=f"sage-worker-{worker.hostname}",
          type="A",
      )

      if infra_stale:
        tunnel = await run_in_executor_with_context(self.cloudflare.get_tunnel_token)

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
                "TUNNEL_TOKEN": tunnel.token,
            },
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
            {"DOMAIN": domain, "HOSTNAME": worker.hostname},
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

      # Land the cert before any container start/restart below
      repaired = []
      if (self.certs.has_valid_certificates()
              and revisions.get("certs") != self.certs.certificates_hash()):
        await self.sync_certificates_to_worker(worker)
        repaired.append("certificates")

      if infra_stale:
        # Start containers
        await self.tailscale.exec_command(
            worker.hostname,
            f"docker compose -f {self.worker_home_dir}/docker-compose.yml up -d --wait --remove-orphans --quiet-pull --quiet-build",
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

      Worker.update(online=True).where(
          Worker.hostname == worker.hostname).execute()

      # Request a resync for exactly the applications whose routing stamp on
      # this worker is stale (covers hosted and mesh files alike).
      for application in Application.select():
        app_hash = routing_input_hash(
            domain, list(application.domains), list(application.containers))
        if revisions.get(application.qualified_name) != app_hash:
          self.request_application_traefik_sync(application)
          repaired.append(application.qualified_name)

      await self.cleanup_orphan_containers(worker)

      if infra_stale:
        # Success commit marker
        await self.write_worker_revision(worker.hostname, "infra", self.infra_hash())
        self.notify(f"Worker {worker.hostname} {'added' if is_new else 'synced'}.")
      else:
        self.notify(
            f"Worker {worker.hostname} converged with infra unchanged"
            + (f"; repaired: {', '.join(repaired)}." if repaired else "; nothing stale."))
    except Exception as e:
      action = "setup" if is_new else "sync"
      self.notify(f"Failed to {action} worker {worker.hostname} : {e}", "error")
      # Only roll back the worker row + DNS if this was a fresh add.
      if is_new:
        await run_in_executor_with_context(self.remove_worker, worker.hostname)
      raise Exception(f"Failed to {action} worker {worker.hostname} : {e}")

  def remove_worker(self, worker_hostname: str):
    """
    A removed worker is assumed to have been destoryed externally (VM + Tailscale) -
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
    if worker.containers.count() > 0:
      logger.error(f"Worker {worker_hostname} gained containers before removal; aborting.")
      raise TaskFailed()

    logger.info(f"Removing worker {worker.hostname} from manager.")
    Worker.delete().where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.delete_dns_record(
        name=f"*.int.{Settings().get('cloudflare', 'domain')}",
        content=worker.ip,
    )
    logger.info(f"Worker {worker.hostname} removed from manager.")

  def set_worker_offline(self, worker):
    """
    A worker can be set to offline if it is expected to come back soon, e.g. after a reboot -
    - Mark as offline in Manager database
    - Delete Cloudflare DNS entry (*.int) for Tailscale routing
    - Cloudflare will automatically handle an offline tunnel
    """
    logger.info(f"Setting worker {worker.hostname} offline.")
    Worker.update(online=False).where(Worker.hostname == worker.hostname).execute()
    self.cloudflare.delete_dns_record(
        name=f"*.int.{Settings().get('cloudflare', 'domain')}",
        content=worker.ip,
    )
    self.notify(f"Worker {worker.hostname} is offline.", "error")
