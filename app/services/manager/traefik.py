import asyncio
import hashlib
import json
import logging
import shlex

import config
from services.db import Domain, Worker
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict
from utils.retry import is_transient, retry

from ._common import CERTIFICATE_TEMPLATES, app_dir

logger = logging.getLogger(__name__)


class TraefikMixin:
  def request_certificate_renewal(self):
    self.add_task(
        task=self.renew_certificates,
        scopes={"platform", "app", "domain"},
        executor="platform",
        on_conflict=OnConflict.REPLACE,
    )

  def setup_domain(self):
    """Before Sage serves: connect to Cloudflare (zone, tunnel, the manager's record) and
    make sure the manager has its certificate, retrying a cold start before giving up.
    Blocking; raises when the domain can't be set up."""
    logger.info(
        f"Setting up {self.cloudflare.manager_hostname}: Cloudflare (zone, tunnel, DNS record), then the "
        "manager's certificate. The UI starts once this is done; a first start can take a few minutes.")
    try:
      retry(
          self.cloudflare.connect, delays=config.STARTUP_RETRY_DELAYS,
          should_retry=lambda error: is_transient(error) and not self.certs.manager.is_valid(),
          on_retry=lambda error, delay: logger.warning(f"Cloudflare setup failed during startup: {error}. Retrying in {delay}s."))
    except Exception as e:
      # With a certificate that still works, serve now; Cloudflare is retried on use.
      if not self.certs.manager.is_valid():
        raise
      logger.error(f"Cloudflare setup failed during startup: {e}. Continuing on the current certificate.")
      return
    self.certs.ensure_manager_at_startup()

  async def renew_certificates(self, force_sync: bool = False):
    failures = []
    self.certs.set_workers({worker.hostname: worker.subdomain for worker in Worker.select()})
    stored = await run_in_executor_with_context(self.certs.stored)
    try:
      await self.sync_certificates_to_workers()
    except Exception as error:
      failures.append(str(error))
    blocked = set()
    for identity, certificate in stored.items():
      if not self.certs.is_current(identity, certificate):
        continue
      try:
        await run_in_executor_with_context(self.certs.retire, certificate)
      except Exception as error:
        blocked.add(identity)
        failures.append(f"{identity} retirement: {error}")
      self.update_workers({})
    for identity, certificate in self.certs.inventory().items():
      if not certificate.names or identity in blocked or not self.certs.is_current(identity, certificate):
        continue
      try:
        if await run_in_executor_with_context(self.certs.ensure, certificate):
          if identity == "manager":
            self.certs.reload_local_tls()
          self.notify(f"Issued certificate for {', '.join(certificate.names)}; expires {certificate.expiry()}.", "success")
      except Exception as error:
        failures.append(f"{identity} issuance: {error}")
      self.update_workers({})
    try:
      await self.sync_certificates_to_workers(force=force_sync)
    except Exception as error:
      failures.append(str(error))
    if failures:
      self.notify("Certificate maintenance failed: " + "; ".join(failures), "error")
      raise RuntimeError("; ".join(failures))

  async def refresh_traefik(self):
    """Maintain certificate inventory and resync worker TLS, Traefik, DNS and routing."""
    try:
      await self.renew_certificates(force_sync=True)
      domain = self.cloudflare.domain

      # Worker Traefik picks up config.yml from its watched dynamic dir (file
      # provider), so no restart is needed.
      for worker in self.online_workers():
        await self.tailscale.sync_file(
            worker.hostname,
            app_dir / "templates/worker/traefik/config.yml",
            f"{self.worker_home_dir}/traefik/dynamic/config.yml",
            {"DOMAIN": domain, "HOSTNAME": worker.hostname},
        )
        await run_in_executor_with_context(self.cloudflare.sync_worker_records, worker.hostname, worker.subdomain, worker.ip)

      self.request_domain_syncs(Domain.select())
      self.request_discovery_sync()
    except Exception as e:
      self.notify(
          f"Traefik refresh failed part-way: {e}. Run Resync Traefik on the Cloudflare settings page "
          "to bring the manager and workers back in sync.",
          "error")
      raise

  async def sync_certificates_to_workers(self, force: bool = False):
    results = await asyncio.gather(*[self.sync_certificates_to_worker(worker, force=force)
                                     for worker in self.online_workers()], return_exceptions=True)
    failures = [str(result) for result in results if isinstance(result, Exception)]
    if failures:
      raise RuntimeError("Worker certificate sync failed: " + "; ".join(failures))

  async def sync_certificates_to_worker(self, worker: Worker, force: bool = False):
    hostname = worker.hostname
    dynamic = f"{self.worker_home_dir}/traefik/dynamic/certs.yml"
    managed = f"{self.worker_home_dir}/traefik/certs/managed"
    revisions, listing = await asyncio.gather(
        self.read_worker_revisions(hostname),
        self.tailscale.exec_command(hostname, f"if [ -f {shlex.quote(dynamic)} ]; then printf '@config '; "
                                    f"sha256sum {shlex.quote(dynamic)} | cut -d ' ' -f 1; fi; "
                                    f"if [ -d {shlex.quote(managed)} ]; then find {shlex.quote(managed)} -mindepth 1 -maxdepth 1 -printf '%f\\n'; fi",
                                    timeout=30))
    actual = set(listing[1])
    configured = next((line.removeprefix("@config ") for line in actual if line.startswith("@config ")), None)
    actual = {line for line in actual if not line.startswith("@config ")}
    material = {role: (certificate, certificate.info(CERTIFICATE_TEMPLATES))
                for role, certificate in self.certs.for_worker(hostname).items() if certificate.is_valid()}
    proofs = {role: info["proof"] for role, (_, info) in material.items()}
    expected = {f"{role}-{info['hash']}.{suffix}" for role, (_, info) in material.items() for suffix in ("pem", "key")}
    certificates = [{"certFile": f"/etc/traefik/certs/managed/{role}-{info['hash']}.pem",
                     "keyFile": f"/etc/traefik/certs/managed/{role}-{info['hash']}.key"} for role, (_, info) in material.items()]
    rendered = (app_dir / "templates/worker/traefik/certs.yml").read_text().replace("${CERTIFICATES}", json.dumps(certificates))
    if (not force and configured == hashlib.sha256(rendered.encode()).hexdigest() and actual == expected
            and self.certs.read_proofs(revisions.get("certs")) == proofs):
      self.update_worker(hostname, certs=proofs)
      return False
    for role, (certificate, info) in material.items():
      for source, suffix, mode in ((certificate.fullchain_path, "pem", 0o644), (certificate.key_path, "key", 0o600)):
        name = f"{role}-{info['hash']}.{suffix}"
        if force or name not in actual:
          await self.tailscale.sync_file(hostname, source, f"{managed}/{name}", mode=mode)
    current = {role: certificate.info(CERTIFICATE_TEMPLATES)["proof"] for role, certificate in self.certs.for_worker(hostname).items()
               if certificate.is_valid()}
    if current != proofs:
      raise RuntimeError(f"{hostname}: certificate configuration changed during sync")
    await self.tailscale.sync_file(hostname, app_dir / "templates/worker/traefik/certs.yml", dynamic,
                                   {"CERTIFICATES": json.dumps(certificates)})
    if actual - expected:
      await self.tailscale.exec_command(hostname, "rm -rf -- " + " ".join(
          shlex.quote(f"{managed}/{name}") for name in sorted(actual - expected)))
    await self.write_worker_revision(hostname, "certs", json.dumps(proofs, sort_keys=True))
    self.update_worker(hostname, certs=proofs)
    return True
