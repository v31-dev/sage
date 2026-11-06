import stat
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from acme import messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from services.certs import Certificate, Certs
from services.db import Worker
from services.manager._common import CERTIFICATE_TEMPLATES
from services.manager.settings import SETTING_SECTIONS, SettingsMixin
from services.manager.traefik import TraefikMixin
from services.manager.workers import WorkersMixin
from services.updates import Updates
from tests.services.manager.test_traefik import LocalWorkers
from tests.services.test_certs import write_certificate
from utils.executor import active_executor


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
    own = catalog.workers["w1"].info(CERTIFICATE_TEMPLATES)
    state = catalog.worker_state("w1", {"worker": own["proof"]}, CERTIFICATE_TEMPLATES)
    assert state["worker"] == {"covered": True, "synced": True, "expiry": own["expiry"], "error": None}
    assert state["discovery"] == {"covered": True, "synced": False, "expiry": None, "error": None}
    assert not catalog.worker_state("w2", {"worker": own["proof"]}, CERTIFICATE_TEMPLATES)["worker"]["synced"]
    assert not catalog.worker_state("w1", {"worker": own["proof"]}, "other-template")["worker"]["synced"]

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

  def test_repeated_edits_retain_revocation_targets_across_a_restart(self, catalog):
    catalog.set_workers({"w1": "red"})
    certificate = catalog.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
    public = certificate.fullchain_path.read_bytes()
    catalog.set_workers({"w1": "blue"})
    catalog.set_workers({"w1": "red"})

    restarted = Certs.__new__(Certs)
    Certs.__init__(restarted)
    restarted.set_workers({"w1": "red"})
    current = restarted.workers["w1"]
    assert not current.is_valid()
    assert not current.key_path.exists()
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

  def test_settings_report_a_failed_renewal_even_when_the_leaf_remains_valid(self, catalog, monkeypatch):
    for certificate in catalog.inventory().values():
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      catalog.reload(certificate)
    catalog.discovery.error = "order rejected"
    manager = SimpleNamespace(
        certs=catalog, setting_checks={key: {"state": "ok", "message": "Provider reachable"} for key in SETTING_SECTIONS},
        applying_tasks=lambda: dict.fromkeys(SETTING_SECTIONS), section_configured=lambda key: True,
        cloudflare=SimpleNamespace(account_id="a", tunnel_id="t", tunnel_name="platform", tunnel_status="healthy"))
    monkeypatch.setattr(type(catalog.directory), "read_bytes", lambda self: pytest.fail("settings read certificate files"))
    status = SettingsMixin.settings_status(manager)["cloudflare"]
    assert status["state"] == "error"
    assert "order rejected" in status["message"]
    assert status["details"]["certificates"][1]["valid"]


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


class Applying(TraefikMixin):
  worker_home_dir = "/opt/sage"
  read_worker_revisions = WorkersMixin.read_worker_revisions
  write_worker_revision = WorkersMixin.write_worker_revision

  def __init__(self, root, catalog):
    self.tailscale = LocalWorkers(root)
    self.certs = catalog
    self.online = {"w1", "w2"}
    self.applied = {}
    self.notices = []

  def online_workers(self):
    return [worker for worker in Worker.select().order_by(Worker.hostname) if worker.hostname in self.online]

  def update_worker(self, hostname, **inputs):
    self.applied[hostname] = inputs["certs"]

  def update_workers(self, inputs):
    pass

  def notify(self, message, type="info"):
    self.notices.append((type, message))


@pytest.fixture
def applying(db, catalog, tmp_path):
  Worker.create(hostname="w1", ip="100.64.0.1", subdomain="red")
  Worker.create(hostname="w2", ip="100.64.0.2", subdomain="blue")
  catalog.set_workers({"w1": "red", "w2": "blue"})
  for certificate in catalog.inventory().values():
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    catalog.reload(certificate)
  return Applying(tmp_path / "remote", catalog)


class TestWorkerCertificateApplication:
  async def test_only_own_and_shared_pairs_are_present_and_the_key_is_private(self, applying):
    for worker in applying.online_workers():
      assert await applying.sync_certificates_to_worker(worker)
      home = applying.tailscale.home(worker.hostname)
      managed = home / "traefik/certs/managed"
      assert len(list(managed.iterdir())) == 4
      keys = {path.read_bytes() for path in managed.glob("*.key")}
      assert keys == {certificate.key_path.read_bytes() for certificate in applying.certs.for_worker(worker.hostname).values()}
      assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in managed.glob("*.key"))
      config = yaml.safe_load((home / "traefik/dynamic/certs.yml").read_text())
      assert len(config["tls"]["certificates"]) == 2
      state = applying.certs.worker_state(worker.hostname, applying.applied[worker.hostname], CERTIFICATE_TEMPLATES)
      assert all(certificate["synced"] for certificate in state.values())

  @pytest.mark.parametrize("drift", ["key", "extra", "config", "references"])
  async def test_matching_proofs_still_repair_missing_or_unwanted_artifacts(self, applying, drift):
    worker = applying.online_workers()[0]
    await applying.sync_certificates_to_worker(worker)
    assert not await applying.sync_certificates_to_worker(worker)
    home = applying.tailscale.home(worker.hostname)
    managed = home / "traefik/certs/managed"
    if drift == "key":
      next(managed.glob("worker-*.key")).unlink()
    elif drift == "extra":
      (managed / "unwanted.key").write_text("obsolete")
    elif drift == "references":
      (home / "traefik/dynamic/certs.yml").write_text("tls:\n  certificates: []\n")
    else:
      (home / "traefik/dynamic/certs.yml").unlink()
    assert await applying.sync_certificates_to_worker(worker)
    assert len(list(managed.iterdir())) == 4
    assert (home / "traefik/dynamic/certs.yml").exists()
    assert not await applying.sync_certificates_to_worker(worker)

  async def test_a_rename_removes_the_old_namespace_even_before_a_new_leaf_is_ready(self, applying):
    worker = applying.online_workers()[0]
    await applying.sync_certificates_to_worker(worker)
    applying.certs.set_workers({"w1": "green", "w2": "blue"})
    await applying.sync_certificates_to_worker(worker)
    home = applying.tailscale.home(worker.hostname)
    assert not list((home / "traefik/certs/managed").glob("worker-*"))
    assert set(applying.applied[worker.hostname]) == {"discovery"}
    assert len(yaml.safe_load((home / "traefik/dynamic/certs.yml").read_text())["tls"]["certificates"]) == 1

  async def test_an_edit_during_material_copy_cannot_commit_a_stale_proof(self, applying, monkeypatch):
    worker = applying.online_workers()[0]
    original = applying.tailscale.sync_file

    async def edit(hostname, source, destination, *args, **kwargs):
      await original(hostname, source, destination, *args, **kwargs)
      if "/worker-" in destination and destination.endswith(".key"):
        applying.certs.set_workers({"w1": "green", "w2": "blue"})

    monkeypatch.setattr(applying.tailscale, "sync_file", edit)
    with pytest.raises(RuntimeError, match="changed during sync"):
      await applying.sync_certificates_to_worker(worker)
    assert not (applying.tailscale.home(worker.hostname) / "revisions/certs").exists()


class TestGlobalMaintenance:
  async def test_an_offline_workers_old_namespace_is_removed_when_it_returns(self, applying, monkeypatch):
    worker = applying.online_workers()[0]
    await applying.sync_certificates_to_worker(worker)
    applying.online.remove("w1")
    Worker.update(subdomain="green").where(Worker.hostname == "w1").execute()
    monkeypatch.setattr(applying.certs, "acme_client", lambda: (SimpleNamespace(revoke=lambda *args: None), None))

    def issue(certificate):
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      applying.certs.reload(certificate)

    monkeypatch.setattr(applying.certs, "issue", issue)
    old = applying.applied["w1"]
    with ThreadPoolExecutor(max_workers=1) as pool:
      token = active_executor.set(pool)
      try:
        await applying.renew_certificates()
      finally:
        active_executor.reset(token)
    assert not applying.certs.worker_state("w1", old, CERTIFICATE_TEMPLATES)["worker"]["synced"]
    applying.online.add("w1")
    await applying.sync_certificates_to_worker(worker)
    home = applying.tailscale.home(worker.hostname)
    state = applying.certs.worker_state("w1", applying.applied["w1"], CERTIFICATE_TEMPLATES)
    assert state["worker"]["synced"]
    assert len(list((home / "traefik/certs/managed").glob("*.pem"))) == 2
    own = next((home / "traefik/certs/managed").glob("worker-*.pem"))
    assert x509.load_pem_x509_certificate(own.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == ["*.green.int.example.test"]

  async def test_retirement_failure_blocks_only_its_replacement_and_issuance_is_serial(self, applying, monkeypatch):
    Worker.update(subdomain="green").where(Worker.hostname == "w1").execute()
    applying.certs.set_workers({"w1": "green", "w2": "blue"})
    for certificate in (applying.certs.discovery, applying.certs.workers["w2"]):
      certificate.fullchain_path.unlink()

    def fail(*args):
      raise RuntimeError("CA offline")

    monkeypatch.setattr(applying.certs, "acme_client", lambda: (SimpleNamespace(revoke=fail), None))
    issued = []
    running = 0
    maximum = 0

    def issue(certificate):
      nonlocal running, maximum
      running += 1
      maximum = max(maximum, running)
      time.sleep(0.01)
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      applying.certs.reload(certificate)
      issued.append(certificate.names)
      running -= 1

    monkeypatch.setattr(applying.certs, "issue", issue)
    with ThreadPoolExecutor(max_workers=3) as pool:
      token = active_executor.set(pool)
      try:
        with pytest.raises(RuntimeError, match="CA offline"):
          await applying.renew_certificates()
      finally:
        active_executor.reset(token)
    assert maximum == 1
    assert issued == [("discovery.int.example.test",), ("*.blue.int.example.test",)]
    assert list(applying.certs.workers["w1"].retired_directory.glob("*.pem"))
    assert not applying.certs.workers["w1"].fullchain_path.exists()
    assert any(type == "error" and "CA offline" in message for type, message in applying.notices)

  async def test_a_failed_renewal_keeps_the_current_key_and_published_config(self, applying, monkeypatch):
    worker = applying.online_workers()[0]
    certificate = applying.certs.workers[worker.hostname]
    write_certificate(certificate.fullchain_path.parent, certificate.names, timedelta(days=3))
    applying.certs.reload(certificate)
    await applying.sync_certificates_to_worker(worker)
    home = applying.tailscale.home(worker.hostname)
    config = (home / "traefik/dynamic/certs.yml").read_text()
    key = certificate.key_path.read_bytes()

    def fail(certificate):
      raise RuntimeError("order rejected")

    monkeypatch.setattr(applying.certs, "issue", fail)
    with ThreadPoolExecutor(max_workers=1) as pool:
      token = active_executor.set(pool)
      try:
        with pytest.raises(RuntimeError, match="order rejected"):
          await applying.renew_certificates()
      finally:
        active_executor.reset(token)
    assert certificate.is_valid()
    assert certificate.key_path.read_bytes() == key
    assert (home / "traefik/dynamic/certs.yml").read_text() == config
    assert applying.certs.worker_state(worker.hostname, applying.applied[worker.hostname], CERTIFICATE_TEMPLATES)["worker"]["synced"]
