"""The certificates' renewal, which keeps each certificate issued for the names it must cover and retires what no
longer is, and their distribution, which copies each worker's to it, with the requests that queue them; and the setup
of the manager's domain and certificate before Sage serves."""
import hashlib
import json
import logging

import config
from services.certs import TEMPLATE_DIGEST, Certs
from services.cloudflare import Cloudflare
from services.db.models import Worker
from services.notification import Notifications
from services.tailscale import Tailscale
from services.workers.state import Workers
from utils.common import fill
from utils.executor import run_in_executor_with_context
from utils.logging import TaskFailed
from utils.queue import OnConflict
from utils.retry import is_transient, retry

logger = logging.getLogger(__name__)


class CertificatesMixin:
  def setup_domain(self):
    """Before Sage serves: connect to Cloudflare (zone, tunnel, the manager's record) and make sure the manager has its
    certificate, retrying a cold start before giving up. Blocking; raises when the domain can't be set up."""
    cloudflare, certs = Cloudflare(), Certs()
    logger.info(
        f"Setting up {cloudflare.manager_hostname}: Cloudflare (zone, tunnel, DNS record), then the "
        "manager's certificate. The UI starts once this is done; a first start can take a few minutes.")
    try:
      retry(
          cloudflare.connect, delays=config.STARTUP_RETRY_DELAYS,
          should_retry=lambda error: is_transient(error) and not certs.manager.is_valid(),
          on_retry=lambda error, delay: logger.warning(f"Cloudflare setup failed during startup: {error}. Retrying in {delay}s."))
    except Exception as e:
      # With a certificate that still works, serve now; Cloudflare is retried on use.
      if not certs.manager.is_valid():
        raise
      logger.error(f"Cloudflare setup failed during startup: {e}. Continuing on the current certificate.")
      return
    certs.ensure_manager_at_startup()

  def request_certificate_renewal(self):
    """Queues the renewal of every certificate. A request while one runs gets one of its own after it."""
    self.add_task(task=self.renew_certificates, key=("renew_certificates",), scopes={"certificate"}, executor="platform",
                  on_conflict=OnConflict.REPLACE)

  async def renew_certificates(self):
    """Sets the names each worker's certificate must cover from the Worker rows; then, one certificate at a time,
    retires what no longer covers its names, a removed worker's included, revoking its public leaf, and issues what is
    missing or due, reloading the manager's certificate in place when it was issued. A retirement that fails holds
    back that certificate's issue only. Then each worker reachable and set up gets its copy brought up to date."""
    certs = Certs()
    certs.set_workers({worker.hostname: worker.subdomain for worker in Worker.select(Worker.hostname, Worker.subdomain)})
    failures = []
    for identity, certificate in (await run_in_executor_with_context(certs.stored)).items():
      try:
        issued = await run_in_executor_with_context(certs.ensure if certificate.names else certs.retire, certificate)
      except Exception as error:
        failures.append(f"{', '.join(certificate.names) or identity}: {error}")
        continue
      if issued:
        if identity == "manager":
          certs.reload_local_tls()
        Notifications().notify(f"Issued the certificate for {', '.join(certificate.names)}; it expires on "
                               f"{certificate.expiry():%Y-%m-%d}.", "success", "/cloudflare")
    for worker in Worker.select(Worker.hostname):
      if Tailscale().ssh(worker.hostname) == "reachable" and Workers().setup(worker.hostname) == "succeeded":
        self.request_certificate_distribution(worker.hostname)
    self.worker_state.check()
    if failures:
      Notifications().notify("Certificates could not all be renewed: " + "; ".join(failures), "error", "/cloudflare")
      raise TaskFailed()

  def request_certificate_distribution(self, hostname: str, force: bool = False):
    """Queues the copy of the worker's certificates to it. It holds the worker's share of the certificates, so their
    renewal excludes it, and the worker's certificate files, so its setup, reboot or removal does; workers go on in
    parallel. Forced, the files are written whatever the worker holds, as a visible task of its own."""
    self.add_task(task=self.distribute_certificates, key=("distribute_certificates", hostname, *(("force",) if force else ())),
                  scopes={f"certificate:worker:{hostname}", f"worker:{hostname}:tls"}, executor="app",
                  params={"hostname": hostname, "force": force}, labels={"Worker": hostname},
                  on_conflict=OnConflict.REPLACE, quiet=not force)

  async def distribute_certificates(self, hostname: str, force: bool = False):
    """Brings the worker's copy of its own and the shared discovery certificate in line with Sage's: each valid pair
    as `traefik/certs/managed/<role>-<hash>.{pem,key}` (the key private), then `traefik/dynamic/certs.yml` listing them,
    then the other managed files removed, then its `certs` stamp with their proofs. A certificate not valid is not
    published. Nothing is written while the stamp, the files and the list are already what they should be, unless
    forced; nothing on a worker not reachable and set up."""
    if (Worker.get_or_none(Worker.hostname == hostname) is None or Tailscale().ssh(hostname) != "reachable"
            or Workers().setup(hostname) != "succeeded"):
      logger.info(f"Worker {hostname} is not there to copy certificates to; nothing to do.")
      return
    dynamic, managed = f"{config.WORKER_HOME_DIR}/traefik/dynamic/certs.yml", f"{config.WORKER_HOME_DIR}/traefik/certs/managed"
    material = {role: (certificate, certificate.info(TEMPLATE_DIGEST))
                for role, certificate in Certs().for_worker(hostname).items() if certificate.is_valid()}
    files = {f"{role}-{info['hash']}.{suffix}": (source, mode) for role, (certificate, info) in material.items()
             for suffix, source, mode in (("pem", certificate.fullchain_path, 0o644), ("key", certificate.key_path, 0o600))}
    certificates = json.dumps([{"certFile": f"/etc/traefik/certs/managed/{role}-{info['hash']}.pem",
                                "keyFile": f"/etc/traefik/certs/managed/{role}-{info['hash']}.key"} for role, (_, info) in material.items()])
    template = config.TEMPLATES_DIR / "worker/traefik/certs.yml"
    proofs = {role: info["proof"] for role, (_, info) in material.items()}
    try:
      revisions = await Workers().read_revisions(hostname)
      _, lines = await Tailscale().exec_command(
          hostname, f"if [ -f {dynamic} ]; then printf '@config '; sha256sum {dynamic} | cut -d ' ' -f 1; fi; "
          f"if [ -d {managed} ]; then find {managed} -mindepth 1 -maxdepth 1 -printf '%f\\n'; fi", timeout=30)
      configured = next((line.removeprefix("@config ") for line in lines if line.startswith("@config ")), None)
      present = {line for line in lines if not line.startswith("@config ")}
      if (not force and present == files.keys() and Certs.read_proofs(revisions.get("certs")) == proofs
              and configured == hashlib.sha256(fill(template.read_text(), {"CERTIFICATES": certificates}).encode()).hexdigest()):
        Workers().record_certificates(hostname, proofs)
        return
      for name, (source, mode) in files.items():
        if force or name not in present:
          await Tailscale().sync_file(hostname, source, f"{managed}/{name}", mode=mode)
      await Tailscale().sync_file(hostname, template, dynamic, {"CERTIFICATES": certificates})
      if stale := sorted(present - files.keys()):
        await Tailscale().exec_command(hostname, "rm -rf -- " + " ".join(f"{managed}/{name}" for name in stale), timeout=30)
      await Workers().write_revision(hostname, "certs", json.dumps(proofs, sort_keys=True))
      Workers().record_certificates(hostname, proofs)
    except Exception as error:
      Notifications().notify(f"The certificates could not be copied to worker {hostname}: {error}", "error", f"/workers/{hostname}")
      raise TaskFailed()
    finally:
      self.worker_state.check()
    logger.info(f"Certificates copied to {hostname}: {', '.join(sorted(proofs)) or 'none valid'}.")
