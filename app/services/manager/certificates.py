"""The certificates' renewal, which keeps each certificate issued for the names it must cover and retires what no
longer is, with the request that queues it; and the setup of the manager's domain and certificate before Sage serves."""
import logging

import config
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.db.models import Worker
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
    back that certificate's issue only."""
    certs = Certs()
    certs.set_workers({worker.hostname: worker.subdomain for worker in Worker.select(Worker.hostname, Worker.subdomain)})
    failures = []
    for identity, certificate in (await run_in_executor_with_context(certs.stored)).items():
      try:
        issued = await run_in_executor_with_context(certs.ensure if certificate.names else certs.retire, certificate)
      except Exception as error:
        failures.append(f"{identity}: {error}")
        continue
      if issued:
        if identity == "manager":
          certs.reload_local_tls()
        # TODO(36 step 4H): self.notify(f"Issued certificate for {', '.join(certificate.names)}; expires {certificate.expiry()}.", "success")
    # TODO(36 step 4F): request every worker's certificate distribution.
    if failures:
      logger.error("Certificate maintenance failed: " + "; ".join(failures))
      # TODO(36 step 4H): self.notify("Certificate maintenance failed: " + "; ".join(failures), "error")
      raise TaskFailed()
