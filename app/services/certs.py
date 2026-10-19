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
from services.cloudflare import Cloudflare
from utils.common import get_env

logger = logging.getLogger(__name__)

ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
RENEWAL_THRESHOLD_DAYS = 30
DNS_PROPAGATION_TIMEOUT = 180
DNS_POLL_INTERVAL = 5
FINALIZE_TIMEOUT_SECONDS = 180
CHALLENGE_COMMENT = "sage-acme-challenge"
# A cold start without a certificate retries before giving up (seconds).
STARTUP_RETRY_DELAYS = (60, 240, 960)


class Certificate:
  """One issued certificate: the names it must cover and its PEM files."""

  def __init__(self, directory: Path, names: list[str]):
    directory.mkdir(parents=True, exist_ok=True)
    self.names = names
    self.fullchain_path = directory / "fullchain.pem"
    self.key_path = directory / "key.pem"

  def _leaf(self):
    if not self.fullchain_path.exists():
      return None
    try:
      return x509.load_pem_x509_certificate(self.fullchain_path.read_bytes())
    except Exception:
      return None

  def expiry(self):
    cert = self._leaf()
    return cert.not_valid_after_utc if cert else None

  def is_valid(self):
    """Unexpired and covering every name (so a domain change makes it stale)."""
    cert = self._leaf()
    if cert is None or cert.not_valid_after_utc <= datetime.now(timezone.utc):
      return False
    try:
      sans = cert.extensions.get_extension_for_class(
          x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
      return False
    return all(name in sans for name in self.names)

  def needs_renewal(self, days: int = RENEWAL_THRESHOLD_DAYS):
    cert = self._leaf()
    if cert is None:
      return True
    return cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=days)

  def hash(self):
    """Content hash of the chain (None when absent); compared against a worker's
    `revisions/certs` stamp to detect missed rotations."""
    if not self.fullchain_path.exists():
      return None
    return hashlib.sha256(self.fullchain_path.read_bytes()).hexdigest()[:16]


class Certs(Base):
  """Owns Sage's two TLS certificates — in-process ACME issuance (Let's Encrypt
  DNS-01 via the Cloudflare client), PEM storage, renewal, and the :443 in-place
  reload. The manager's certificate covers only its own address and never leaves the
  manager; the workers' covers `*.int.<domain>` and is synced to every worker."""

  def __init__(self):
    super().__init__()

    cert_dir = Path("/app/data/certs")
    cert_dir.mkdir(parents=True, exist_ok=True)
    self.account_key_path = cert_dir / "account.key"
    self.domain = get_env("DOMAIN")
    self.manager = Certificate(cert_dir / "manager", [Cloudflare().manager_hostname])
    self.workers = Certificate(cert_dir / "workers", [f"*.int.{self.domain}"])

    # The uvicorn :443 SSLContext, set by main once the server is built. Holding
    # the reference lets renewal swap the cert in place (new handshakes get the
    # new cert, live connections keep theirs) without restarting the server.
    self.tls_context = None

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

  def issue(self, certificate: Certificate):
    """Run one ACME DNS-01 order for the certificate's names and persist the PEM
    cert + key. Blocking (network I/O plus a DNS propagation wait) — offload to
    an executor lane, never call on the event loop."""
    account_key = self._account_key()
    net = client.ClientNetwork(account_key, user_agent="sage")
    directory = client.ClientV2.get_directory(ACME_DIRECTORY, net)
    acme_client = client.ClientV2(directory, net=net)
    try:
      # No contact email: Let's Encrypt doesn't send expiry mail.
      acme_client.new_account(messages.NewRegistration.from_data(terms_of_service_agreed=True))
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
    order = acme_client.new_order(crypto_util.make_csr(cert_key_pem, certificate.names))

    # One DNS-01 authorization per identifier: clear any stale record, publish
    # the TXT, wait for it to be visible on the authoritative nameservers, then
    # answer all challenges and finalize.
    published = []
    try:
      for authz in order.authorizations:
        challenge = next(c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01))
        response, validation = challenge.chall.response_and_validation(account_key)
        record_name = challenge.chall.validation_domain_name(authz.body.identifier.value)
        Cloudflare().delete_dns_records(record_name, type="TXT")
        Cloudflare().create_dns_record(
            name=record_name, content=validation, comment=CHALLENGE_COMMENT, type="TXT")
        published.append((record_name, validation, challenge, response))

      # Let's Encrypt validates a challenge the moment it is answered, so wait
      # until every TXT is actually served before answering
      logger.info(f"Waiting for the DNS-01 records to appear on the zone's nameservers (up to {DNS_PROPAGATION_TIMEOUT}s).")
      self._wait_for_dns([(name, value) for name, value, _, _ in published])

      for _, _, challenge, response in published:
        acme_client.answer_challenge(challenge, response)

      logger.info(f"Let's Encrypt is validating the DNS-01 challenge (up to {FINALIZE_TIMEOUT_SECONDS}s).")
      order = acme_client.poll_and_finalize(
          order, deadline=datetime.now() + timedelta(seconds=FINALIZE_TIMEOUT_SECONDS))
    finally:
      for record_name, *_ in published:
        try:
          Cloudflare().delete_dns_records(record_name, type="TXT")
        except Exception as e:
          logger.warning(f"Failed to remove ACME challenge record {record_name}: {e}")

    certificate.fullchain_path.write_text(order.fullchain_pem)
    certificate.key_path.write_bytes(cert_key_pem)
    certificate.key_path.chmod(0o600)
    logger.info(f"Issued certificate for {', '.join(certificate.names)}; expires {certificate.expiry()}.")

  def _wait_for_dns(self, records):
    """Block until every (name, expected_value) TXT is served by the zone's
    authoritative nameservers — the same place Let's Encrypt queries — so a
    challenge is never answered before it can be seen. Raises on timeout."""
    ns_ips = []
    try:
      for ns in dns.resolver.resolve(self.domain, "NS"):
        for record_type in ("A", "AAAA"):
          try:
            ns_ips.extend(ip.to_text() for ip in dns.resolver.resolve(ns.target, record_type))
          except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
    except dns.exception.DNSException as e:
      logger.warning(f"Could not resolve nameservers for {self.domain}: {e}.")

    if ns_ips:
      resolver = dns.resolver.Resolver(configure=False)
      resolver.nameservers = ns_ips
    else:
      # The system resolver caches, so a negative answer can outlive the record
      # and stall the poll until it times out.
      logger.warning(
          f"No authoritative nameservers resolved for {self.domain}; falling back to the "
          "system resolver to observe DNS-01 records.")
      resolver = dns.resolver.Resolver()
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

  def ensure(self, certificate: Certificate):
    """Issue only when there is no valid cert or it is within the renewal
    window. Returns True if a new cert was written. Blocking — offload."""
    if certificate.is_valid() and not certificate.needs_renewal():
      return False
    logger.info(f"Requesting a certificate for {', '.join(certificate.names)} from Let's Encrypt.")
    self.issue(certificate)
    return True

  def ensure_manager_at_startup(self):
    """Sage serves only on the manager's certificate, so a cold start retries with
    backoff before giving up. An existing valid certificate means this was only a
    renewal: a failure is logged and left to the daily renewal task rather than
    holding startup for a certificate that still works."""
    for delay in (*STARTUP_RETRY_DELAYS, None):
      try:
        self.ensure(self.manager)
        return
      except Exception as e:
        if self.manager.is_valid():
          logger.error(
              f"Certificate renewal failed during startup: {e}. Continuing on the current "
              f"certificate (expires {self.manager.expiry()}); the daily renewal will retry.",
              exc_info=True)
          return
        # Let's Encrypt's Retry-After for a rate limit is measured in hours, so
        # further attempts this session would fail regardless.
        if delay is None or (isinstance(e, messages.Error) and e.code == "rateLimited"):
          raise
        logger.warning(f"Certificate issuance failed during startup: {e}. Retrying in {delay}s.")
        time.sleep(delay)

  def reload_local_tls(self):
    """Swap the :443 cert in place on the live SSLContext. Call on the event
    loop (same thread that serves the TLS handshakes)."""
    if self.tls_context is None:
      return
    self.tls_context.load_cert_chain(self.manager.fullchain_path, self.manager.key_path)
    logger.info("Reloaded :443 TLS certificate in place.")
