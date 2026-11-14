import logging
import stat
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
from services.certs import Certificate, Certs, certificate_files_scope, certificate_scope
from services.updates import Updates
from services.workers.rendering import stack_scope, worker_scope
from utils.queue import _scope_conflict

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
  (directory / "key.pem").write_bytes(key.private_bytes(
      serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
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

  def test_a_certificate_with_more_names_than_asked_for_is_not_valid(self, tmp_path):
    write_certificate(tmp_path, ["a.example.test", "b.example.test"])
    assert Certificate(tmp_path, ["a.example.test"]).is_valid() is False

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
  @pytest.mark.parametrize("seconds, renew", [(-1, True), (0, True), (1, False)])
  def test_seven_days_before_the_signed_expiry(self, tmp_path, clock, seconds, renew):
    issued = write_certificate(tmp_path, NAMES, timedelta(days=45))
    clock.set((issued.not_valid_after_utc - timedelta(days=7, seconds=seconds)).replace(tzinfo=None))
    assert Certificate(tmp_path, NAMES).needs_renewal() is renew

  def test_the_default_window_reads_current_config(self, tmp_path, monkeypatch):
    write_certificate(tmp_path, NAMES, timedelta(days=10))
    certificate = Certificate(tmp_path, NAMES)
    assert certificate.needs_renewal() is False

    monkeypatch.setattr(config, "RENEWAL_THRESHOLD_DAYS", 14)

    assert certificate.needs_renewal() is True

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

  def test_it_is_that_of_the_file_last_loaded(self, tmp_path):
    (tmp_path / "fullchain.pem").write_bytes(b"one")
    certificate = Certificate(tmp_path, NAMES)
    first = certificate.hash()
    (tmp_path / "fullchain.pem").write_bytes(b"two")
    certificate.load()
    assert certificate.hash() != first


class TestACMEProfile:
  @pytest.mark.parametrize("meta", [{}, {"profiles": {}}, {"profiles": {"classic": "https://example.test/classic"}}])
  def test_an_unadvertised_profile_records_failure_without_replacing_material(self, tmp_path, data_dir, monkeypatch, meta):
    write_certificate(tmp_path, NAMES, timedelta(days=3))
    certificate = Certificate(tmp_path, NAMES)
    key = certificate.key_path.read_bytes()
    fullchain = certificate.fullchain_path.read_bytes()
    certs = Certs()
    directory = messages.Directory.from_json({"meta": meta})
    monkeypatch.setattr("services.certs.client.ClientV2.get_directory", lambda *_: directory)

    with pytest.raises(ValueError, match="tlsserver"):
      certs.ensure(certificate)

    assert "tlsserver" in certificate.error
    assert certificate.fullchain_path.read_bytes() == fullchain
    assert certificate.key_path.read_bytes() == key
    assert certificate.is_valid() is True


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


TEMPLATES = "templates"


@pytest.fixture
def catalog(data_dir, clock, register):
  clock.set(datetime.now(timezone.utc).replace(tzinfo=None))
  register(Updates, SimpleNamespace(changed=lambda source: None))
  return Certs()


class TestCertificateInventory:
  def test_each_worker_has_only_its_namespace_and_the_shared_discovery_identity(self, catalog):
    catalog.set_workers({"w1": "red", "w2": "blue"})
    for certificate in catalog.inventory().values():
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      catalog.reload(certificate)

    assert catalog.manager.names == ("manager.core.example.test",)
    assert catalog.discovery.names == ("discovery.int.example.test",)
    assert {role: certificate.names for role, certificate in catalog.for_worker("w1").items()} == {
        "discovery": ("discovery.int.example.test",), "worker": ("*.red.int.example.test",)}
    own = catalog.workers["w1"].info(TEMPLATES)
    state = catalog.worker_state("w1", "red", {"worker": own["proof"]}, TEMPLATES)
    assert state["worker"] == {"covered": True, "synced": True, "expiry": own["expiry"], "error": None}
    assert state["discovery"] == {"covered": True, "synced": False, "expiry": None, "error": None}
    assert not catalog.worker_state("w2", "blue", {"worker": own["proof"]}, TEMPLATES)["worker"]["synced"]
    assert not catalog.worker_state("w1", "red", {"worker": own["proof"]}, "other-template")["worker"]["synced"]

  def test_issuance_generates_a_distinct_private_key_for_each_owner(self, catalog, monkeypatch):
    catalog.set_workers({"w1": "red", "w2": "blue"})
    ca_key = ec.generate_private_key(ec.SECP256R1())

    def order(csr, **kwargs):
      request = x509.load_pem_x509_csr(csr)
      now = datetime.now(timezone.utc)
      leaf = (x509.CertificateBuilder().subject_name(request.subject).issuer_name(request.subject)
              .public_key(request.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=45))
              .add_extension(request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False)
              .sign(ca_key, hashes.SHA256()))
      return SimpleNamespace(authorizations=[], fullchain_pem=leaf.public_bytes(serialization.Encoding.PEM).decode())

    ca = SimpleNamespace(new_order=order, poll_and_finalize=lambda order, **kwargs: order)
    monkeypatch.setattr(catalog, "acme_client", lambda profile: (ca, None))
    monkeypatch.setattr(catalog, "_wait_for_dns", lambda records: None)
    for certificate in catalog.inventory().values():
      catalog.issue(certificate)
    assert len({certificate.key_path.read_bytes() for certificate in catalog.inventory().values()}) == 4
    assert all(certificate.is_valid() for certificate in catalog.inventory().values())
    assert all(stat.S_IMODE(certificate.key_path.stat().st_mode) == 0o600 for certificate in catalog.inventory().values())

  @pytest.mark.parametrize("key", [None, "other"])
  def test_a_missing_or_mismatched_key_cannot_be_published(self, catalog, tmp_path, key):
    write_certificate(catalog.discovery.fullchain_path.parent, catalog.discovery.names)
    if key:
      write_certificate(tmp_path, ["other.example.test"])
      catalog.discovery.key_path.write_bytes((tmp_path / "key.pem").read_bytes())
    else:
      catalog.discovery.key_path.unlink()
    catalog.reload(catalog.discovery)
    assert not catalog.discovery.is_valid()
    assert catalog.discovery.info()["proof"] is None

  def test_a_rename_leaves_the_chain_in_place_obsolete_and_renaming_back_makes_it_valid_again(self, catalog):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
    public = certificate.fullchain_path.read_bytes()

    catalog.set_workers({"w1": "blue"})

    renamed = catalog.workers["w1"]
    assert renamed.fullchain_path.read_bytes() == public and renamed.key_path.exists()
    assert renamed.obsolete() and not renamed.is_valid()
    assert not renamed.retired_directory.exists()

    catalog.set_workers({"w1": "red"})
    assert catalog.workers["w1"].is_valid()
    assert not catalog.workers["w1"].retired_directory.exists()

  def test_retiring_a_renamed_chain_keeps_its_public_leaf_for_revocation_across_a_restart(self, catalog, monkeypatch):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
    public = certificate.fullchain_path.read_bytes()
    catalog.set_workers({"w1": "blue"})

    def fail(*args):
      raise RuntimeError("CA unavailable")

    monkeypatch.setattr(catalog, "acme_client", lambda: (SimpleNamespace(revoke=fail), None))
    with pytest.raises(RuntimeError, match="CA unavailable"):
      catalog.retire(catalog.workers["w1"])

    restarted = Certs.__new__(Certs)
    Certs.__init__(restarted)
    restarted.set_workers({"w1": "blue"})
    current = restarted.workers["w1"]
    assert not current.is_valid() and not current.key_path.exists()
    assert [path.read_bytes() for path in current.retired_directory.glob("*.pem")] == [public]

  def test_removed_workers_are_found_on_disk_without_adopting_a_broad_certificate(self, catalog):
    orphan = Certificate(catalog.directory / "workers" / "gone", ["*.gone.int.example.test"])
    write_certificate(orphan.fullchain_path.parent, orphan.names)
    write_certificate(catalog.directory / "workers", ["*.int.example.test"])
    inventory = catalog.stored()
    assert inventory["worker:gone"].names == ()
    assert inventory["worker:gone"].obsolete()
    assert set(inventory) == {"manager", "discovery", "worker:gone"}

  def test_a_rename_while_the_ca_is_answering_retains_the_result_for_revocation(self, catalog, tmp_path, monkeypatch):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(tmp_path, certificate.names)
    public = (tmp_path / "fullchain.pem").read_text()

    def finalize(*args, **kwargs):
      catalog.set_workers({"w1": "blue"})
      return SimpleNamespace(fullchain_pem=public)

    ca = SimpleNamespace(new_order=lambda *args, **kwargs: SimpleNamespace(authorizations=[]), poll_and_finalize=finalize)
    monkeypatch.setattr(catalog, "acme_client", lambda profile: (ca, None))
    monkeypatch.setattr(catalog, "_wait_for_dns", lambda records: None)
    with pytest.raises(RuntimeError, match="ownership changed"):
      catalog.ensure(certificate)
    assert catalog.workers["w1"].names == ("*.blue.int.example.test",)
    assert not catalog.workers["w1"].is_valid()
    assert not certificate.key_path.exists()
    assert [path.read_text() for path in certificate.retired_directory.glob("*.pem")] == [public]

  @pytest.mark.parametrize("value", [None, "invalid", "[]", '{"worker": 1}'])
  def test_an_invalid_stamp_proves_nothing(self, value):
    assert Certs.read_proofs(value) == {}

  def test_after_a_subdomain_change_the_wildcard_is_not_covered_until_the_renewal(self, catalog):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
    proof = certificate.info(TEMPLATES)["proof"]

    assert catalog.worker_state("w1", "red", {"worker": proof}, TEMPLATES)["worker"]["covered"]
    moved = catalog.worker_state("w1", "blue", {"worker": proof}, TEMPLATES)["worker"]
    assert moved == {"covered": False, "synced": False, "expiry": None, "error": None}
    assert certificate.fullchain_path.exists()


class TestRetirement:
  def test_a_local_cleanup_failure_is_retained_in_certificate_status(self, catalog, monkeypatch):
    write_certificate(catalog.discovery.fullchain_path.parent, ["old.example.test"])
    catalog.reload(catalog.discovery)

    def fail():
      raise OSError("read-only filesystem")

    monkeypatch.setattr(catalog.discovery, "stash", fail)
    with pytest.raises(OSError, match="read-only filesystem"):
      catalog.retire(catalog.discovery)
    assert catalog.discovery.error == "read-only filesystem"

  def test_failed_revocation_keeps_only_the_public_leaf_until_retry(self, catalog, monkeypatch):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
    catalog.set_workers({})
    removed = catalog.workers["w1"]

    def fail(*args):
      raise RuntimeError("CA unavailable")

    monkeypatch.setattr(catalog, "acme_client", lambda: (SimpleNamespace(revoke=fail), None))
    with pytest.raises(RuntimeError, match="CA unavailable"):
      catalog.retire(removed)
    assert not certificate.key_path.exists()
    assert not certificate.fullchain_path.exists()
    assert len(list(certificate.retired_directory.glob("*.pem"))) == 1
    assert "CA unavailable" in removed.error

    monkeypatch.setattr(catalog, "acme_client", lambda: (SimpleNamespace(revoke=lambda cert, reason: None), None))
    catalog.retire(removed)
    assert "w1" not in catalog.workers
    assert not certificate.fullchain_path.parent.exists()

  def test_an_already_revoked_leaf_is_successful_cleanup(self, catalog, monkeypatch):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
    catalog.set_workers({})

    def revoked(*args):
      raise messages.Error(typ="urn:ietf:params:acme:error:alreadyRevoked", detail="already revoked")

    monkeypatch.setattr(catalog, "acme_client", lambda: (SimpleNamespace(revoke=revoked), None))
    catalog.retire(catalog.workers["w1"])
    assert "w1" not in catalog.workers

  def test_expired_retired_material_needs_no_ca(self, catalog, monkeypatch):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names, timedelta(days=-1))
    catalog.reload(certificate)
    catalog.set_workers({})
    monkeypatch.setattr(catalog, "acme_client", lambda: pytest.fail("expired certificate contacted CA"))
    catalog.retire(catalog.workers["w1"])
    assert "w1" not in catalog.workers


@pytest.mark.parametrize("hostname", ["..", "a/b", "."])
def test_a_hostname_that_is_not_one_directory_is_refused(catalog, hostname):
  with pytest.raises(ValueError, match="Invalid worker hostname."):
    catalog.set_workers({hostname: "red"})


def test_a_worker_without_a_subdomain_gets_no_certificate(catalog):
  catalog.set_workers({"w1": None})
  assert "w1" not in catalog.workers


def test_the_same_names_again_keep_the_certificate_as_it_is(catalog):
  catalog.set_workers({"w1": "red"})
  certificate = catalog.workers["w1"]
  catalog.set_workers({"w1": "red"})
  assert catalog.workers["w1"] is certificate


def test_every_certificate_covers_each_workers_and_a_workers_certificate_files_are_under_the_worker():
  assert _scope_conflict(certificate_scope(), certificate_scope("w1"))
  assert not _scope_conflict(certificate_scope("w1"), certificate_scope("w2"))
  assert _scope_conflict(worker_scope("w1"), certificate_files_scope("w1"))
  assert not any(_scope_conflict(certificate_files_scope("w1"), scope)
                 for scope in (stack_scope("w1"), certificate_scope("w1"), certificate_files_scope("w2")))
