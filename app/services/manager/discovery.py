"""Discovery's publication: each worker's discovery files, from the Domains with a service and what their owners hold,
and the discovery DNS records, with the requests that queue them."""
import logging

import config
from services.cloudflare import Cloudflare
from services.db.models import Domain, Worker
from services.domains.discovery import discovery_files, discovery_files_scope, discovery_scope, discovery_services, files_command, parse_file_listing
from services.notification import Notifications
from services.tailscale import Tailscale
from services.workers.decisions import discovery_member
from services.workers.state import Workers
from utils.common import get_env, utcnow
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed
from utils.queue import OnConflict

logger = logging.getLogger(__name__)


class DiscoveryMixin:
  def request_discovery(self, hostname: str | None = None, force: bool = False):
    """Queues the publication of discovery on every worker reachable and set up, or on `hostname` only, each under its
    own scope so the worker's setup, reboot or removal excludes it, and of its DNS records. A request while one runs
    gets one of its own after it. Forced, the files are written whatever the worker holds, as a visible task of its
    own."""
    workers = Worker.select(Worker.hostname)
    if hostname is not None:
      workers = workers.where(Worker.hostname == hostname)
    for worker in workers:
      if Tailscale().ssh(worker.hostname) == "reachable" and Workers().setup(worker.hostname) == "succeeded":
        self.add_task(task=self.publish_discovery, key=("publish_discovery", worker.hostname, *(("force",) if force else ())),
                      scopes={discovery_files_scope(worker.hostname)}, executor="app",
                      params={"hostname": worker.hostname, "force": force}, labels={"Worker": worker.hostname},
                      on_conflict=OnConflict.REPLACE, quiet=not force)
    self.add_task(task=self.publish_discovery_dns, key=("publish_discovery_dns",), scopes={discovery_scope()}, executor="app",
                  on_conflict=OnConflict.REPLACE, quiet=True)

  async def publish_discovery(self, hostname: str, force: bool = False):
    """Writes each of the worker's discovery files whose first line does not name the fingerprint its service gives
    now (every one, forced), then removes those of services no Domain has. A name whose availability is not known yet
    keeps what the worker's file publishes for it (`discovery_services`). Nothing on a worker not reachable and set
    up."""
    if (Worker.get_or_none(Worker.hostname == hostname) is None or Tailscale().ssh(hostname) != "reachable"
            or Workers().setup(hostname) != "succeeded"):
      logger.info(f"Worker {hostname} is not there to publish discovery to; nothing to do.")
      return
    domains = list(Domain.select().where(Domain.service.is_null(False)))
    states = self.domain_states(domains)
    dynamic = f"{config.WORKER_HOME_DIR}/traefik/dynamic"
    try:
      _, lines = await Tailscale().exec_command(hostname, files_command(dynamic), timeout=30)
      present = parse_file_listing(lines)
      files = discovery_files(discovery_services(
          ((domain, states[domain.id]) for domain in domains),
          {host: online for file in present.values() for host, online in file.online.items()}, utcnow(), self.started_at),
          get_env("DOMAIN"))
      written = [name for name, (fingerprint, _) in files.items()
                 if force or name not in present or present[name].proof != fingerprint]
      for name in written:
        await Tailscale().sync_file(hostname, config.TEMPLATES_DIR / "worker/file", f"{dynamic}/{name}", {"CONTENT": files[name][1]})
      if stale := sorted(present.keys() - files.keys()):
        await Tailscale().exec_command(hostname, "rm -f " + " ".join(f"{dynamic}/{name}" for name in stale), timeout=30)
    except Exception as error:
      Notifications().notify(f"Discovery could not be published on worker {hostname}: {error}", "error", f"/workers/{hostname}")
      raise TaskFailed()
    logger.info(f"Discovery on {hostname}: {len(written)} file(s) written, {len(stale)} removed.")

  async def publish_discovery_dns(self):
    """Points `discovery.int.<domain>` at every worker reachable and set up, one record each under
    `sage-discovery-<hostname>`, and `discovery.<domain>` at the platform's tunnel, from one list of the zone. The
    record of a worker known to be out (`discovery_member`), or removed, is deleted; that of one not known yet is left
    as it is."""
    since_start = (utcnow() - self.started_at).total_seconds()
    members = {worker.hostname: discovery_member(Tailscale().ssh(worker.hostname), Workers().setup(worker.hostname),
                                                 Tailscale().address(worker.hostname), since_start)
               for worker in Worker.select(Worker.hostname)}
    cloudflare = Cloudflare()
    try:
      await run_in_executor_with_context(cloudflare.connect)
      zone = await run_in_executor_with_context(cloudflare.get_dns_records)
      internal = {comment: {} for record in zone if (comment := record.get("comment") or "").startswith("sage-discovery-")
                  and members.get(comment.removeprefix("sage-discovery-"), False) is False}
      internal.update({f"sage-discovery-{hostname}": {f"discovery.int.{cloudflare.domain}": Tailscale().address(hostname)}
                       for hostname, member in members.items() if member})
      await run_in_executor_with_context(cloudflare.sync_dns_records, internal, zone=zone, type="A", proxied=False)
      await run_in_executor_with_context(
          cloudflare.sync_dns_records, {"sage-tunnel": {f"discovery.{cloudflare.domain}": f"{cloudflare.tunnel_id}.cfargotunnel.com"}},
          zone=zone, type="CNAME", proxied=True)
    except Exception as error:
      Notifications().notify(f"The discovery DNS records could not be published: {error}", "error", "/domains")
      raise TaskFailed()
