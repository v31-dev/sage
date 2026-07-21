import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dns.exception
import dns.resolver
import josepy as jose
from acme import challenges, client, crypto_util, errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.base import Base
from services.db import Worker
from services.settings import Settings

app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)

ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
RENEWAL_THRESHOLD_DAYS = 30
DNS_PROPAGATION_TIMEOUT = 180
DNS_POLL_INTERVAL = 5
FINALIZE_TIMEOUT_SECONDS = 180
CHALLENGE_COMMENT = "sage-acme-challenge"


class Certs(Base):
  """Owns the wildcard TLS certificate: in-process ACME issuance (Let's Encrypt
  DNS-01 via the Cloudflare client), PEM storage sage owns directly, renewal,
  the :443 in-place cert hot-reload, and delivery to worker Traefiks."""

  def __init__(self, manager):
    super().__init__()

    self.manager = manager
    self.cert_dir = Path("/app/data/certs")
    self.cert_dir.mkdir(parents=True, exist_ok=True)
    self.fullchain_path = self.cert_dir / "fullchain.pem"
    self.key_path = self.cert_dir / "key.pem"
    self.account_key_path = self.cert_dir / "account.key"

    # The uvicorn :443 SSLContext, set by main once the server is built. Holding
    # the reference lets renewal swap the cert in place (new handshakes get the
    # new cert, live connections keep theirs) without restarting the server.
    self.tls_context = None

    self.load()
    self.ensure()

  def load(self):
    self.domain = Settings().get("cloudflare", "domain")
    self.admin_email = Settings().get("cloudflare", "admin_email")

    if not self.domain or not self.admin_email:
      raise ValueError("Platform settings require non-empty domain and admin_email values.")

  def _domains(self):
    return [self.domain, f"*.core.{self.domain}", f"*.int.{self.domain}"]

  def _account_key(self):
    if self.account_key_path.exists():
      key = serialization.load_pem_private_key(self.account_key_path.read_bytes(), password=None)
    else:
      key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
      self.account_key_path.write_bytes(
          key.private_bytes(
              serialization.Encoding.PEM,
              serialization.PrivateFormat.PKCS8,
              serialization.NoEncryption(),
          )
      )
      self.account_key_path.chmod(0o600)
    return jose.JWKRSA(key=key)

  def issue(self):
    """Run one ACME DNS-01 order for the wildcard SAN set and persist the PEM
    cert + key. Blocking (network I/O plus a DNS propagation wait) — offload to
    an executor lane, never call on the event loop."""
    account_key = self._account_key()
    net = client.ClientNetwork(account_key, user_agent="sage")
    directory = client.ClientV2.get_directory(ACME_DIRECTORY, net)
    acme_client = client.ClientV2(directory, net=net)
    try:
      acme_client.new_account(
          messages.NewRegistration.from_data(email=self.admin_email, terms_of_service_agreed=True)
      )
    except errors.ConflictError as e:
      # The account key is already registered
      acme_client.net.account = messages.RegistrationResource(
          body=messages.Registration(), uri=e.location)

    cert_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert_key_pem = cert_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    order = acme_client.new_order(crypto_util.make_csr(cert_key_pem, self._domains()))

    # One DNS-01 authorization per identifier: clear any stale record, publish
    # the TXT, wait for it to be visible on the authoritative nameservers, then
    # answer all challenges and finalize.
    published = []
    try:
      for authz in order.authorizations:
        challenge = next(c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01))
        response, validation = challenge.chall.response_and_validation(account_key)
        record_name = challenge.chall.validation_domain_name(authz.body.identifier.value)
        self.manager.cloudflare.delete_dns_records(record_name, type="TXT")
        self.manager.cloudflare.create_dns_record(
            name=record_name, content=validation, comment=CHALLENGE_COMMENT, type="TXT")
        published.append((record_name, validation, challenge, response))

      # Let's Encrypt validates a challenge the moment it is answered, so wait
      # until every TXT is actually served before answering
      self._wait_for_dns([(name, value) for name, value, _, _ in published])

      for _, _, challenge, response in published:
        acme_client.answer_challenge(challenge, response)

      order = acme_client.poll_and_finalize(
          order, deadline=datetime.now() + timedelta(seconds=FINALIZE_TIMEOUT_SECONDS))
    finally:
      for record_name, *_ in published:
        try:
          self.manager.cloudflare.delete_dns_records(record_name, type="TXT")
        except Exception as e:
          logger.warning(f"Failed to remove ACME challenge record {record_name}: {e}")

    self.fullchain_path.write_text(order.fullchain_pem)
    self.key_path.write_bytes(cert_key_pem)
    self.key_path.chmod(0o600)
    logger.info(f"Issued wildcard certificate for {self.domain}; expires {self.expiry()}.")

  def _wait_for_dns(self, records):
    """Block until every (name, expected_value) TXT is served by the zone's
    authoritative nameservers — the same place Let's Encrypt queries — so a
    challenge is never answered before it can be seen. Raises on timeout."""
    ns_ips = []
    for ns in dns.resolver.resolve(self.domain, "NS"):
      for record_type in ("A", "AAAA"):
        try:
          ns_ips.extend(ip.to_text() for ip in dns.resolver.resolve(ns.target, record_type))
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
          continue

    resolver = dns.resolver.Resolver()
    if ns_ips:
      resolver = dns.resolver.Resolver(configure=False)
      resolver.nameservers = ns_ips
    resolver.lifetime = 10

    deadline = time.time() + DNS_PROPAGATION_TIMEOUT
    pending = list(records)
    while pending:
      remaining = []
      for name, value in pending:
        try:
          answers = resolver.resolve(name, "TXT")
          visible = any(value == txt.decode() for rdata in answers for txt in rdata.strings)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.Timeout):
          visible = False
        if not visible:
          remaining.append((name, value))
      pending = remaining
      if pending and time.time() >= deadline:
        raise TimeoutError(
            f"DNS-01 TXT records not visible on authoritative nameservers after "
            f"{DNS_PROPAGATION_TIMEOUT}s: {[name for name, _ in pending]}")
      if pending:
        time.sleep(DNS_POLL_INTERVAL)

  def ensure(self):
    """Issue only when there is no valid cert or it is within the renewal
    window. Returns True if a new cert was written. Blocking — offload."""
    if self.has_valid_certificates() and not self.needs_renewal():
      return False
    self.issue()
    return True

  def _leaf_cert(self):
    if not self.fullchain_path.exists():
      return None
    try:
      return x509.load_pem_x509_certificate(self.fullchain_path.read_bytes())
    except Exception:
      return None

  def expiry(self):
    cert = self._leaf_cert()
    return cert.not_valid_after_utc if cert else None

  def has_valid_certificates(self):
    cert = self._leaf_cert()
    if cert is None or cert.not_valid_after_utc <= datetime.now(timezone.utc):
      return False
    try:
      sans = cert.extensions.get_extension_for_class(
          x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
      return False
    return self.domain in sans

  def needs_renewal(self, days: int = RENEWAL_THRESHOLD_DAYS):
    cert = self._leaf_cert()
    if cert is None:
      return True
    return cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=days)

  def certificates_hash(self):
    """Content hash of the issued cert chain (None when absent); compared
    against a worker's `revisions/certs` stamp to detect missed rotations."""
    if not self.fullchain_path.exists():
      return None
    return hashlib.sha256(self.fullchain_path.read_bytes()).hexdigest()[:16]

  def reload_local_tls(self):
    """Swap the :443 cert in place on the live SSLContext. Call on the event
    loop (same thread that serves the TLS handshakes)."""
    if self.tls_context is None:
      return
    self.tls_context.load_cert_chain(self.fullchain_path, self.key_path)
    logger.info("Reloaded :443 TLS certificate in place.")

  async def sync_certificates_to_workers(self, force: bool = False):
    if not self.has_valid_certificates():
      self.manager.notify(
          "No valid certificate available; skipping certificate sync to workers.",
          type="error",
      )
      return

    cert_hash = self.certificates_hash()
    targets = []
    for worker in Worker.select().where(Worker.online):
      if force:
        targets.append(worker)
      else:
        revisions = await self.manager.read_worker_revisions(worker.hostname)
        if revisions.get("certs") != cert_hash:
          targets.append(worker)

    # Workers are independent, so sync them concurrently: the asyncssh SFTP
    # leaves fan out and drain on the event loop.
    await asyncio.gather(
        *[self.sync_certificates_to_worker(worker) for worker in targets],
        return_exceptions=False,
    )

  async def sync_certificates_to_worker(self, worker: Worker):
    traefik_dir = f"{self.manager.worker_home_dir}/traefik"
    await self.manager.tailscale.sync_file(
        worker.hostname, self.fullchain_path, f"{traefik_dir}/certs/fullchain.pem")
    await self.manager.tailscale.sync_file(
        worker.hostname, self.key_path, f"{traefik_dir}/certs/key.pem")
    # Re-write the watched dynamic cert config so Traefik's file-provider watch
    # fires and reloads the new PEM; overwriting only the cert files is not a
    # reliable reload trigger.
    await self.manager.tailscale.sync_file(
        worker.hostname,
        app_dir / "templates/worker/traefik/certs.yml",
        f"{traefik_dir}/dynamic/certs.yml")
    await self.manager.write_worker_revision(
        worker.hostname, "certs", self.certificates_hash() or "")
    logger.info(f"Synced certificate to worker {worker.hostname}.")
