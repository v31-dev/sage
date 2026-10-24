import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from acme import messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import config
from services.certs import Certificate, Certs

NAMES = ["manager.core.example.test"]


def write_certificate(directory, names, expires_in=timedelta(days=90), with_san=True):
  """A self-signed fullchain.pem in `directory` covering `names`, valid until `expires_in` from now."""
  key = ec.generate_private_key(ec.SECP256R1())
  now = datetime.now(timezone.utc)
  subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
  builder = (x509.CertificateBuilder()
             .subject_name(subject).issuer_name(subject).public_key(key.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - timedelta(days=90))
             .not_valid_after(now + expires_in))
  if with_san:
    builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
  certificate = builder.sign(key, hashes.SHA256())
  (directory / "fullchain.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
  return certificate


class TestWithoutACertificate:
  def test_the_directory_is_created(self, tmp_path):
    directory = tmp_path / "certs" / "workers"
    Certificate(directory, NAMES)
    assert directory.is_dir()

  def test_it_is_not_valid_and_needs_issuing(self, tmp_path):
    certificate = Certificate(tmp_path, NAMES)
    assert certificate.expiry() is None
    assert certificate.is_valid() is False
    assert certificate.needs_renewal() is True
    assert certificate.hash() is None

  def test_an_unreadable_file_counts_as_no_certificate(self, tmp_path):
    (tmp_path / "fullchain.pem").write_text("not a certificate")
    certificate = Certificate(tmp_path, NAMES)
    assert certificate.expiry() is None
    assert certificate.is_valid() is False
    assert certificate.needs_renewal() is True


class TestLoad:
  def test_expiry_is_the_end_date_of_the_certificate(self, tmp_path):
    written = write_certificate(tmp_path, NAMES, timedelta(days=10))
    assert Certificate(tmp_path, NAMES).expiry() == written.not_valid_after_utc

  def test_the_certificate_is_read_into_memory_until_load_is_called(self, tmp_path):
    certificate = Certificate(tmp_path, NAMES)

    write_certificate(tmp_path, NAMES)
    assert certificate.is_valid() is False

    certificate.load()
    assert certificate.is_valid() is True

  @pytest.mark.parametrize("expires_in, valid", [(timedelta(hours=1), True), (timedelta(hours=-1), False)])
  def test_a_certificate_an_hour_either_side_of_its_expiry(self, tmp_path, expires_in, valid):
    write_certificate(tmp_path, NAMES, expires_in)
    assert Certificate(tmp_path, NAMES).is_valid() is valid

  def test_load_forgets_a_certificate_whose_file_is_gone(self, tmp_path):
    write_certificate(tmp_path, NAMES)
    certificate = Certificate(tmp_path, NAMES)
    (tmp_path / "fullchain.pem").unlink()

    certificate.load()

    assert certificate.expiry() is None


class TestIsValid:
  def test_unexpired_and_covering_every_name(self, tmp_path):
    write_certificate(tmp_path, ["a.example.test", "b.example.test"])
    assert Certificate(tmp_path, ["a.example.test", "b.example.test"]).is_valid() is True

  def test_a_certificate_with_more_names_than_asked_for_is_valid(self, tmp_path):
    write_certificate(tmp_path, ["a.example.test", "b.example.test"])
    assert Certificate(tmp_path, ["a.example.test"]).is_valid() is True

  def test_an_expired_certificate_is_not_valid(self, tmp_path):
    write_certificate(tmp_path, NAMES, timedelta(hours=-1))
    assert Certificate(tmp_path, NAMES).is_valid() is False

  def test_a_name_it_does_not_cover_makes_it_stale(self, tmp_path):
    write_certificate(tmp_path, ["old.example.test"])
    assert Certificate(tmp_path, ["new.example.test"]).is_valid() is False

  def test_every_name_must_be_covered(self, tmp_path):
    write_certificate(tmp_path, ["a.example.test"])
    assert Certificate(tmp_path, ["a.example.test", "b.example.test"]).is_valid() is False

  def test_a_wildcard_is_matched_as_written(self, tmp_path):
    write_certificate(tmp_path, ["*.int.example.test"])
    assert Certificate(tmp_path, ["*.int.example.test"]).is_valid() is True
    assert Certificate(tmp_path, ["app.int.example.test"]).is_valid() is False

  def test_a_certificate_without_names_is_not_valid(self, tmp_path):
    write_certificate(tmp_path, NAMES, with_san=False)
    assert Certificate(tmp_path, NAMES).is_valid() is False


class TestNeedsRenewal:
  def test_outside_the_renewal_window_it_does_not(self, tmp_path):
    write_certificate(tmp_path, NAMES, timedelta(days=config.RENEWAL_THRESHOLD_DAYS + 1))
    assert Certificate(tmp_path, NAMES).needs_renewal() is False

  def test_inside_the_renewal_window_it_does(self, tmp_path):
    write_certificate(tmp_path, NAMES, timedelta(days=config.RENEWAL_THRESHOLD_DAYS - 1))
    assert Certificate(tmp_path, NAMES).needs_renewal() is True

  def test_an_expired_certificate_needs_renewal(self, tmp_path):
    write_certificate(tmp_path, NAMES, timedelta(hours=-1))
    assert Certificate(tmp_path, NAMES).needs_renewal() is True

  @pytest.mark.parametrize("hours, renew", [(-1, True), (1, False)])
  def test_the_edge_of_the_window_to_the_hour(self, tmp_path, hours, renew):
    write_certificate(tmp_path, NAMES, timedelta(days=config.RENEWAL_THRESHOLD_DAYS, hours=hours))
    assert Certificate(tmp_path, NAMES).needs_renewal() is renew

  @pytest.mark.parametrize("days, renew", [(60, True), (40, False)])
  def test_the_window_can_be_given(self, tmp_path, days, renew):
    write_certificate(tmp_path, NAMES, timedelta(days=50))
    assert Certificate(tmp_path, NAMES).needs_renewal(days=days) is renew


class TestHash:
  def test_it_is_the_first_sixteen_characters_of_the_sha256_of_the_file(self, tmp_path):
    (tmp_path / "fullchain.pem").write_bytes(b"abc")
    assert Certificate(tmp_path, NAMES).hash() == "ba7816bf8f01cfea"

  def test_it_follows_the_file_without_a_reload(self, tmp_path):
    certificate = Certificate(tmp_path, NAMES)
    (tmp_path / "fullchain.pem").write_bytes(b"one")
    first = certificate.hash()
    (tmp_path / "fullchain.pem").write_bytes(b"two")
    assert certificate.hash() != first


class TestEnsureManagerAtStartup:
  """A cold start retries with backoff; a certificate that still works ends it at once."""

  @pytest.fixture
  def sleeps(self, monkeypatch):
    delays = []
    monkeypatch.setattr(config, "STARTUP_RETRY_DELAYS", (60, 240))
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays

  def certs(self, results, valid=False):
    """A Certs whose `ensure` returns or raises each of `results` in turn."""
    certs = Certs.__new__(Certs)
    certs.manager = SimpleNamespace(is_valid=lambda: valid, expiry=lambda: "tomorrow")
    certs.calls = 0

    def ensure(certificate):
      outcome = results[certs.calls]
      certs.calls += 1
      if isinstance(outcome, Exception):
        raise outcome
      return outcome

    certs.ensure = ensure
    return certs

  def test_a_certificate_that_is_issued_at_once_waits_for_nothing(self, sleeps):
    certs = self.certs([True])

    certs.ensure_manager_at_startup()

    assert certs.calls == 1
    assert sleeps == []

  def test_a_failure_waits_and_tries_again(self, sleeps, caplog):
    certs = self.certs([Exception("dns not ready"), True])

    with caplog.at_level(logging.WARNING, logger="services.certs"):
      certs.ensure_manager_at_startup()

    assert certs.calls == 2
    assert sleeps == [60]
    assert "Certificate issuance failed during startup: dns not ready. Retrying in 60s." in caplog.text

  def test_it_gives_up_after_the_last_delay_with_the_last_error(self, sleeps):
    certs = self.certs([Exception("first"), Exception("second"), Exception("last")])

    with pytest.raises(Exception, match="last"):
      certs.ensure_manager_at_startup()

    assert certs.calls == 3
    assert sleeps == [60, 240]

  def test_a_rate_limit_is_not_retried(self, sleeps):
    limited = messages.Error(typ="urn:ietf:params:acme:error:rateLimited", detail="too many certificates")
    certs = self.certs([limited, True])

    with pytest.raises(messages.Error):
      certs.ensure_manager_at_startup()

    assert certs.calls == 1
    assert sleeps == []

  def test_another_acme_error_is_retried(self, sleeps):
    certs = self.certs([messages.Error(typ="urn:ietf:params:acme:error:serverInternal", detail="try later"), True])

    certs.ensure_manager_at_startup()

    assert certs.calls == 2
    assert sleeps == [60]

  def test_with_a_certificate_that_still_works_a_failure_is_logged_and_startup_goes_on(self, sleeps, caplog):
    certs = self.certs([Exception("renewal failed")], valid=True)

    with caplog.at_level(logging.ERROR, logger="services.certs"):
      certs.ensure_manager_at_startup()

    assert certs.calls == 1
    assert sleeps == []
    assert "Certificate renewal failed during startup: renewal failed. Continuing on the current certificate (expires tomorrow)" in caplog.text
