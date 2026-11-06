import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dns.exception
import dns.resolver
import josepy as jose
from acme import challenges, client, crypto_util, errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import config
from services.base import Base
from services.cloudflare import Cloudflare
from services.updates import Fact, UpdateSource
from utils.common import get_env, utcnow
from utils.retry import retry

logger = logging.getLogger(__name__)


def _aware_now():
  """utcnow() for comparing with a certificate's aware expiry."""
  return utcnow().replace(tzinfo=timezone.utc)


CHALLENGE_COMMENT = "sage-acme-challenge"


@dataclass(frozen=True)
class CertificateMaterial:
  certificate: x509.Certificate | None = None
  covered: frozenset[str] = frozenset()
  fingerprint: str | None = None
  key_matches: bool = False


class Certificate:
  """One issued certificate: the names it must cover and its PEM files."""

  def __init__(self, directory: Path, names: list[str]):
    directory.mkdir(parents=True, exist_ok=True)
    self.names = tuple(names)
    self.fullchain_path = directory / "fullchain.pem"
    self.key_path = directory / "key.pem"
    self.retired_directory = directory / "retired"
    # The latest issuance or retirement failure.
    self.error = None
    self.material = CertificateMaterial()
    self.load()

  def load(self):
    """Read the certificate into memory, so checks and the status never touch the disk.
    At start, and after every write of the PEM file."""
    certificate, covered, fingerprint, key_matches = None, frozenset(), None, False
    if not self.fullchain_path.exists():
      self.material = CertificateMaterial()
      return
    pem = self.fullchain_path.read_bytes()
    fingerprint = hashlib.sha256(pem).hexdigest()[:16]
    try:
      certificate = x509.load_pem_x509_certificate(pem)
      covered = frozenset(certificate.extensions.get_extension_for_class(
          x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
      key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
      key_matches = certificate.public_key().public_bytes(
          serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == key.public_key().public_bytes(
          serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    except (ValueError, TypeError, OSError, x509.ExtensionNotFound):
      pass
    self.material = CertificateMaterial(certificate, covered, fingerprint, key_matches)

  @property
  def covered(self):
    return self.material.covered

  def expiry(self):
    return self.material.certificate.not_valid_after_utc if self.material.certificate else None

  def is_valid(self):
    return self.info()["valid"]

  def info(self, template: str = "") -> dict:
    material = self.material
    expiry = material.certificate.not_valid_after_utc if material.certificate else None
    valid = bool(self.names and material.key_matches and expiry and expiry > _aware_now()
                 and material.covered == frozenset(self.names))
    return {"names": list(self.names), "valid": valid, "expiry": expiry.isoformat() if expiry else None,
            "error": self.error, "hash": material.fingerprint,
            "proof": hashlib.sha256(f"{material.fingerprint}:{template}".encode()).hexdigest()[:16] if valid else None}

  def obsolete(self) -> bool:
    return not self.names or bool(self.material.certificate and (
        self.material.covered != frozenset(self.names) or not self.material.key_matches))

  def stash(self):
    if self.fullchain_path.exists():
      if self.material.certificate:
        self.retired_directory.mkdir(exist_ok=True)
        self.fullchain_path.replace(self.retired_directory / f"{self.material.fingerprint}.pem")
      else:
        self.fullchain_path.unlink()
    self.key_path.unlink(missing_ok=True)
    self.load()

  def needs_renewal(self, days: int | None = None):
    if self.material.certificate is None:
      return True
    return self.material.certificate.not_valid_after_utc <= _aware_now() + timedelta(days=config.RENEWAL_THRESHOLD_DAYS if days is None else days)

  def hash(self):
    """Content hash of the loaded chain, or None when absent."""
    return self.material.fingerprint


class Certs(Base):
  """Owns certificate material, desired names, ACME issuance and retirement."""

  def __init__(self):
    super().__init__()

    self.directory = config.DATA_DIR / "certs"
    self.directory.mkdir(parents=True, exist_ok=True)
    self.account_key_path = self.directory / "account.key"
    self.domain = get_env("DOMAIN")
    self.manager = Certificate(self.directory / "manager", [Cloudflare().manager_hostname])
    self.discovery = Certificate(self.directory / "discovery", [f"discovery.int.{self.domain}"])
    self.workers: dict[str, Certificate] = {}

    # The uvicorn :443 SSLContext, set by main once the server is built. Holding
    # the reference lets renewal swap the cert in place (new handshakes get the
    # new cert, live connections keep theirs) without restarting the server.
    self.tls_context = None
    self.changed = Fact(self.status, UpdateSource.CERTIFICATES)

  def set_workers(self, workers: dict[str, str | None]):
    with self.lock:
      for hostname in self.workers.keys() | workers.keys():
        if Path(hostname).name != hostname or hostname in {".", ".."}:
          raise ValueError("Invalid worker hostname.")
        names = (f"*.{workers[hostname]}.int.{self.domain}",) if workers.get(hostname) else ()
        if hostname not in self.workers and not names:
          continue
        if hostname not in self.workers or self.workers[hostname].names != names:
          if hostname in self.workers:
            self.workers[hostname].stash()
          self.workers[hostname] = Certificate(self.directory / "workers" / hostname, names)
    self.changed.check()

  def inventory(self) -> dict[str, Certificate]:
    with self.lock:
      return {"manager": self.manager, "discovery": self.discovery,
              **{f"worker:{hostname}": certificate for hostname, certificate in sorted(self.workers.items())}}

  def stored(self) -> dict[str, Certificate]:
    with self.lock:
      if (self.directory / "workers").exists():
        for directory in (self.directory / "workers").iterdir():
          if directory.is_dir() and not directory.is_symlink() and directory.name not in self.workers:
            self.workers[directory.name] = Certificate(directory, ())
      certificates = {"manager": self.manager, "discovery": self.discovery,
                      **{f"worker:{hostname}": certificate for hostname, certificate in sorted(self.workers.items())}}
      for certificate in certificates.values():
        certificate.load()
    self.changed.check()
    return certificates

  def status(self) -> list[dict]:
    return [{"id": identity, **{key: value for key, value in certificate.info().items() if key not in {"hash", "proof"}}}
            for identity, certificate in self.inventory().items()]

  def is_current(self, identity: str, certificate: Certificate) -> bool:
    return self.inventory().get(identity) is certificate

  def for_worker(self, hostname: str) -> dict[str, Certificate]:
    with self.lock:
      return {"discovery": self.discovery,
              **({"worker": self.workers[hostname]} if hostname in self.workers and self.workers[hostname].names else {})}

  def worker_state(self, hostname: str, applied: dict | None, template: str) -> dict:
    certificates = self.for_worker(hostname)
    result = {}
    for role in ("worker", "discovery"):
      info = certificates[role].info(template) if role in certificates else None
      synced = bool(info and info["valid"] and (applied or {}).get(role) == info["proof"])
      result[role] = {"covered": bool(info and info["valid"]), "synced": synced,
                      "expiry": info["expiry"] if synced else None, "error": info["error"] if info else None}
    return result

  @staticmethod
  def read_proofs(value: str | None) -> dict:
    try:
      proofs = json.loads(value or "{}")
      return proofs if isinstance(proofs, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in proofs.items()) else {}
    except (ValueError, TypeError):
      return {}

  def reload(self, certificate: Certificate):
    with self.lock:
      certificate.load()
      for current in (self.manager, self.discovery, *self.workers.values()):
        if current is not certificate and current.fullchain_path == certificate.fullchain_path:
          current.load()
          current.error = certificate.error
    self.changed.check()

  def retire(self, certificate: Certificate):
    try:
      with self.lock:
        obsolete = certificate.obsolete()
        if obsolete:
          certificate.stash()
      pending = sorted(certificate.retired_directory.glob("*.pem"))
      if not obsolete and not pending:
        return
      failures = []
      acme_client = None
      for path in pending:
        try:
          retired = x509.load_pem_x509_certificate(path.read_bytes())
          if retired.not_valid_after_utc > _aware_now():
            if acme_client is None:
              acme_client, _ = self.acme_client()
            try:
              acme_client.revoke(retired, 5)
            except messages.Error as error:
              if error.code != "alreadyRevoked":
                raise
          path.unlink()
        except Exception as error:
          failures.append(f"{path.name}: {error}")
      certificate.error = "; ".join(failures) or None
      self.reload(certificate)
      if failures:
        raise RuntimeError(certificate.error)
      if certificate.retired_directory.exists():
        certificate.retired_directory.rmdir()
      if not certificate.names:
        with self.lock:
          for hostname, current in list(self.workers.items()):
            if not current.names and current.fullchain_path == certificate.fullchain_path:
              current.fullchain_path.parent.rmdir()
              del self.workers[hostname]
      self.changed.check()
    except Exception as error:
      certificate.error = str(error)
      self.reload(certificate)
      raise

  def acme_client(self, profile: str | None = None):
    account_key = self._account_key()
    net = client.ClientNetwork(account_key, user_agent="sage")
    directory = client.ClientV2.get_directory(config.ACME_DIRECTORY, net)
    if profile is not None and profile not in (directory.meta.profiles or {}):
      raise ValueError(f"ACME directory does not advertise the required '{profile}' certificate profile.")
    acme_client = client.ClientV2(directory, net=net)
    try:
      acme_client.new_account(messages.NewRegistration.from_data(terms_of_service_agreed=True))
    except errors.ConflictError as error:
      acme_client.net.account = messages.RegistrationResource(body=messages.Registration(), uri=error.location)
    return acme_client, account_key

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
    acme_client, account_key = self.acme_client(config.ACME_PROFILE)

    cert_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert_key_pem = cert_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    order = acme_client.new_order(crypto_util.make_csr(cert_key_pem, certificate.names), profile=config.ACME_PROFILE)

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
      logger.info(f"Waiting for the DNS-01 records to appear on the zone's nameservers (up to {config.DNS_PROPAGATION_TIMEOUT}s).")
      self._wait_for_dns([(name, value) for name, value, _, _ in published])

      for _, _, challenge, response in published:
        acme_client.answer_challenge(challenge, response)

      logger.info(f"Let's Encrypt is validating the DNS-01 challenge (up to {config.FINALIZE_TIMEOUT_SECONDS}s).")
      # The ACME library compares its deadline with naive local time, so this one stays local.
      order = acme_client.poll_and_finalize(
          order, deadline=datetime.now() + timedelta(seconds=config.FINALIZE_TIMEOUT_SECONDS))  # noqa: DTZ005
    finally:
      for record_name, *_ in published:
        try:
          Cloudflare().delete_dns_records(record_name, type="TXT")
        except Exception as e:
          logger.warning(f"Failed to remove ACME challenge record {record_name}: {e}")

    with self.lock:
      current = next((item for item in self.inventory().values() if item.fullchain_path == certificate.fullchain_path), None)
      if current is not None and current is not certificate:
        certificate.retired_directory.mkdir(exist_ok=True)
        fingerprint = hashlib.sha256(order.fullchain_pem.encode()).hexdigest()[:16]
        (certificate.retired_directory / f"{fingerprint}.pem").write_text(order.fullchain_pem)
        raise RuntimeError("Certificate ownership changed during issuance.")
      certificate.fullchain_path.write_text(order.fullchain_pem)
      certificate.key_path.write_bytes(cert_key_pem)
      certificate.key_path.chmod(0o600)
    self.reload(certificate)
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

    deadline = time.time() + config.DNS_PROPAGATION_TIMEOUT
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
            f"{config.DNS_PROPAGATION_TIMEOUT}s: {[name for name, _ in pending]}")
      if pending:
        time.sleep(config.DNS_POLL_INTERVAL)

  def ensure(self, certificate: Certificate):
    """Issue only when there is no valid cert or it is within the renewal
    window. Returns True if a new cert was written. Blocking — offload."""
    self.retire(certificate)
    if certificate.is_valid() and not certificate.needs_renewal():
      return False
    logger.info(f"Requesting a certificate for {', '.join(certificate.names)} from Let's Encrypt.")
    try:
      self.issue(certificate)
    except Exception as e:
      certificate.error = str(e)
      raise
    else:
      certificate.error = None
    finally:
      self.changed.check()
    return True

  def ensure_manager_at_startup(self):
    """Sage serves only on the manager's certificate, so a cold start retries with
    backoff before giving up. An existing valid certificate means this was only a
    renewal: a failure is logged and left to the daily renewal task rather than
    holding startup for a certificate that still works."""
    def should_retry(error):
      # Let's Encrypt's Retry-After for a rate limit is measured in hours, so further
      # attempts this session would fail regardless.
      rate_limited = isinstance(error, messages.Error) and error.code == "rateLimited"
      return not self.manager.is_valid() and not rate_limited

    try:
      retry(
          lambda: self.ensure(self.manager), delays=config.STARTUP_RETRY_DELAYS, should_retry=should_retry,
          on_retry=lambda error, delay: logger.warning(f"Certificate issuance failed during startup: {error}. Retrying in {delay}s."))
    except Exception as e:
      if not self.manager.is_valid():
        raise
      logger.error(
          f"Certificate renewal failed during startup: {e}. Continuing on the current "
          f"certificate (expires {self.manager.expiry()}); the daily renewal will retry.",
          exc_info=True)

  def reload_local_tls(self):
    """Swap the :443 cert in place on the live SSLContext. Call on the event
    loop (same thread that serves the TLS handshakes)."""
    if self.tls_context is None:
      return
    self.tls_context.load_cert_chain(self.manager.fullchain_path, self.manager.key_path)
    logger.info("Reloaded :443 TLS certificate in place.")
