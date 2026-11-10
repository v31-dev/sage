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


class TestCertificateSettings:
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
