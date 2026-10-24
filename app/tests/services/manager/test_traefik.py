import logging
import time
from types import SimpleNamespace

import pytest

import config
from services.manager.traefik import TraefikMixin


class TestSetupDomain:
  """Cloudflare is connected before Sage serves, retrying a cold start; a certificate that still
  works lets it serve anyway."""

  @pytest.fixture
  def sleeps(self, monkeypatch):
    delays = []
    monkeypatch.setattr(config, "STARTUP_RETRY_DELAYS", (60, 240))
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays

  def manager(self, results, valid=False):
    """What setup_domain needs of the Manager; `connect` returns or raises each of `results`."""
    manager = SimpleNamespace(calls=0, ensured=False)

    def connect():
      outcome = results[manager.calls]
      manager.calls += 1
      if isinstance(outcome, Exception):
        raise outcome

    def ensure_manager_at_startup():
      manager.ensured = True

    manager.cloudflare = SimpleNamespace(manager_hostname="manager.core.example.test", connect=connect)
    manager.certs = SimpleNamespace(manager=SimpleNamespace(is_valid=lambda: valid), ensure_manager_at_startup=ensure_manager_at_startup)
    return manager

  def test_a_connection_that_works_goes_on_to_the_certificate(self, sleeps):
    manager = self.manager([None])

    TraefikMixin.setup_domain(manager)

    assert manager.calls == 1
    assert manager.ensured
    assert sleeps == []

  def test_a_failure_waits_and_tries_again(self, sleeps, caplog):
    manager = self.manager([Exception("zone not found"), None])

    with caplog.at_level(logging.WARNING, logger="services.manager.traefik"):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 2
    assert sleeps == [60]
    assert manager.ensured
    assert "Cloudflare setup failed during startup: zone not found. Retrying in 60s." in caplog.text

  def test_it_gives_up_after_the_last_delay_and_never_asks_for_a_certificate(self, sleeps):
    manager = self.manager([Exception("first"), Exception("second"), Exception("last")])

    with pytest.raises(Exception, match="last"):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 3
    assert sleeps == [60, 240]
    assert not manager.ensured

  def test_with_a_certificate_that_still_works_it_serves_at_once(self, sleeps, caplog):
    manager = self.manager([Exception("zone not found")], valid=True)

    with caplog.at_level(logging.ERROR, logger="services.manager.traefik"):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 1
    assert sleeps == []
    assert not manager.ensured
    assert "Cloudflare setup failed during startup: zone not found. Continuing on the current certificate." in caplog.text


class TestSyncCertificatesToWorker:
  async def test_the_private_key_is_synced_with_a_mode_that_keeps_it_private_and_the_rest_is_not(self):
    calls = []

    async def sync_file(hostname, source, destination, keys=None, formatter=None, timeout=120, mode=None):
      calls.append((destination, mode))

    async def write_worker_revision(hostname, key, value):
      pass

    manager = SimpleNamespace(
        worker_home_dir="/opt/sage",
        tailscale=SimpleNamespace(sync_file=sync_file),
        certs=SimpleNamespace(workers=SimpleNamespace(fullchain_path="fullchain.pem", key_path="key.pem", hash=lambda: "abc")),
        write_worker_revision=write_worker_revision)

    await TraefikMixin.sync_certificates_to_worker(manager, SimpleNamespace(hostname="w1"))

    assert dict(calls) == {
        "/opt/sage/traefik/certs/fullchain.pem": None,
        "/opt/sage/traefik/certs/key.pem": 0o600,
        "/opt/sage/traefik/dynamic/certs.yml": None,
    }
