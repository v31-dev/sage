from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import config
from services.certs import Certificate

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
