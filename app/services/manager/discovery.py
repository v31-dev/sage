"""Discovery's publication: each worker's discovery files, from the Domains with a service and what their owners hold,
and the discovery DNS records, with the requests that queue them."""
import logging

import config
from services.cloudflare import Cloudflare
from services.db.models import Domain, Worker
from services.domains.discovery import PREFIX, SUFFIX, discovery_files, discovery_fingerprint, discovery_services, files_command
from services.notification import Notifications
from services.tailscale import Tailscale
from services.workers.state import Workers
from utils.common import get_env
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
                      scopes={f"worker:{worker.hostname}:discovery"}, executor="app",
                      params={"hostname": worker.hostname, "force": force}, labels={"Worker": worker.hostname},
                      on_conflict=OnConflict.REPLACE, quiet=not force)
    self.add_task(task=self.publish_discovery_dns, key=("publish_discovery_dns",), scopes={"discovery"}, executor="app",
                  on_conflict=OnConflict.REPLACE, quiet=True)

  async def publish_discovery(self, hostname: str, force: bool = False):
    """Writes the worker's discovery files when its stamp or its set of files is not what the services give now, or
    forced: each service's file, then the stale ones removed, then the stamp. Nothing on a worker not reachable and set
    up."""
    if (Worker.get_or_none(Worker.hostname == hostname) is None or Tailscale().ssh(hostname) != "reachable"
            or Workers().setup(hostname) != "succeeded"):
      logger.info(f"Worker {hostname} is not there to publish discovery to; nothing to do.")
      return
    domains = list(Domain.select().where(Domain.service.is_null(False)))
    states = self.domain_states(domains)
    services = discovery_services((domain, states[domain.id]) for domain in domains)
    base_domain, dynamic = get_env("DOMAIN"), f"{config.WORKER_HOME_DIR}/traefik/dynamic"
    files, stamp = discovery_files(services, base_domain), discovery_fingerprint(services, base_domain)
    try:
      revisions = await Workers().read_revisions(hostname)
      _, lines = await Tailscale().exec_command(hostname, files_command(dynamic), timeout=30)
      present = {line for line in lines if line.startswith(PREFIX) and line.endswith(SUFFIX)}
      if not force and present == files.keys() and revisions.get("discovery") == stamp:
        return
      for name, content in files.items():
        await Tailscale().sync_file(hostname, config.TEMPLATES_DIR / "worker/file", f"{dynamic}/{name}", {"CONTENT": content})
      if stale := sorted(present - files.keys()):
        await Tailscale().exec_command(hostname, "rm -f " + " ".join(f"{dynamic}/{name}" for name in stale), timeout=30)
      await Workers().write_revision(hostname, "discovery", stamp)
    except Exception as error:
      Notifications().notify(f"Discovery could not be published on worker {hostname}: {error}", "error", f"/workers/{hostname}")
      raise TaskFailed()
    logger.info(f"Discovery published on {hostname}: {len(services)} service(s).")

  async def publish_discovery_dns(self):
    """Points `discovery.int.<domain>` at every worker reachable and set up, one record each under
    `sage-discovery-<hostname>`, the records of any other worker deleted, and `discovery.<domain>` at the platform's
    tunnel, from one list of the zone."""
    hostnames = [worker.hostname for worker in Worker.select(Worker.hostname)
                 if Tailscale().ssh(worker.hostname) == "reachable" and Workers().setup(worker.hostname) == "succeeded"
                 and Tailscale().address(worker.hostname)]
    cloudflare = Cloudflare()
    try:
      await run_in_executor_with_context(cloudflare.connect)
      zone = await run_in_executor_with_context(cloudflare.get_dns_records)
      internal = {record["comment"]: {} for record in zone if (record.get("comment") or "").startswith("sage-discovery-")}
      internal.update({f"sage-discovery-{hostname}": {f"discovery.int.{cloudflare.domain}": Tailscale().address(hostname)}
                       for hostname in hostnames})
      await run_in_executor_with_context(cloudflare.sync_dns_records, internal, zone=zone, type="A", proxied=False)
      await run_in_executor_with_context(
          cloudflare.sync_dns_records, {"sage-tunnel": {f"discovery.{cloudflare.domain}": f"{cloudflare.tunnel_id}.cfargotunnel.com"}},
          zone=zone, type="CNAME", proxied=True)
    except Exception as error:
      Notifications().notify(f"The discovery DNS records could not be published: {error}", "error", "/domains")
      raise TaskFailed()
