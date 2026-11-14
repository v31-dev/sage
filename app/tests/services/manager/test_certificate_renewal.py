import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509

import config
from services.certs import Certs
from services.cloudflare import Cloudflare, CloudflareError
from services.db.models import Worker
from services.manager.certificates import CertificatesMixin
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from services.updates import Updates
from tests.services.test_certs import write_certificate
from utils.executor import active_executor
from utils.logging import TaskFailed
from utils.queue import TaskQueue

MANAGER = "https://manager.core.example.test"


class TestSetupDomain:
  """Cloudflare is connected before Sage serves, retrying a cold start for failures that clear by themselves; a
  certificate that still works lets it serve anyway."""

  @pytest.fixture
  def sleeps(self, monkeypatch):
    delays = []
    monkeypatch.setattr(config, "STARTUP_RETRY_DELAYS", (60, 240))
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays

  @pytest.fixture
  def setup(self, register):
    """Cloudflare whose `connect` returns or raises each of `results`, and the certificates, valid or not."""
    state = SimpleNamespace(calls=0, ensured=False, results=[None], valid=False)

    def connect():
      outcome = state.results[state.calls]
      state.calls += 1
      if isinstance(outcome, Exception):
        raise outcome

    def ensure_manager_at_startup():
      state.ensured = True

    register(Cloudflare, SimpleNamespace(manager_hostname="manager.core.example.test", connect=connect))
    register(Certs, SimpleNamespace(manager=SimpleNamespace(is_valid=lambda: state.valid),
                                    ensure_manager_at_startup=ensure_manager_at_startup))
    return state

  def test_a_connection_that_works_goes_on_to_the_certificate(self, sleeps, setup):
    CertificatesMixin().setup_domain()
    assert (setup.calls, setup.ensured, sleeps) == (1, True, [])

  @pytest.mark.parametrize("status", [429, 500, 503])
  def test_a_failure_that_clears_waits_and_tries_again(self, sleeps, caplog, setup, status):
    setup.results = [CloudflareError(status, "busy"), None]
    with caplog.at_level(logging.WARNING, logger="services.manager.certificates"):
      CertificatesMixin().setup_domain()
    assert (setup.calls, sleeps, setup.ensured) == (2, [60], True)
    assert f"Cloudflare setup failed during startup: Cloudflare answered HTTP {status}: busy. Retrying in 60s." in caplog.text

  @pytest.mark.parametrize("error", [
      CloudflareError(401, "Invalid API Token"),
      CloudflareError(403, "Forbidden"),
      ValueError("The Cloudflare API token is not active."),
      Exception("zone not found"),
  ])
  def test_a_failure_that_does_not_clear_stops_the_start_at_once(self, sleeps, setup, error):
    setup.results = [error]
    with pytest.raises(type(error)):
      CertificatesMixin().setup_domain()
    assert (setup.calls, sleeps, setup.ensured) == (1, [], False)

  def test_it_gives_up_after_the_last_delay_and_never_asks_for_a_certificate(self, sleeps, setup):
    setup.results = [CloudflareError(503, "first"), CloudflareError(503, "second"), CloudflareError(503, "last")]
    with pytest.raises(CloudflareError, match="last"):
      CertificatesMixin().setup_domain()
    assert (setup.calls, sleeps, setup.ensured) == (3, [60, 240], False)

  @pytest.mark.parametrize("error", [CloudflareError(401, "Invalid API Token"), CloudflareError(503, "busy")])
  def test_with_a_certificate_that_still_works_it_serves_at_once(self, sleeps, caplog, setup, error):
    setup.results, setup.valid = [error], True
    with caplog.at_level(logging.ERROR, logger="services.manager.certificates"):
      CertificatesMixin().setup_domain()
    assert (setup.calls, sleeps, setup.ensured) == (1, [], False)
    assert f"Cloudflare setup failed during startup: {error}. Continuing on the current certificate." in caplog.text


class Manager(CertificatesMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=1)
    self.queue_facts = []
    self.worker_state = SimpleNamespace(check=lambda: None)
    self.task_queue = TaskQueue(SCOPES, {"platform": self.pool}, record=lambda task, status: None, notify=self._queue_changed)


@pytest.fixture
def certs(data_dir, clock, register):
  clock.set(datetime.now(timezone.utc).replace(tzinfo=None))
  register(Updates, SimpleNamespace(changed=lambda source, key=None: None, publish=lambda feed, rows, key=None: None))
  return Certs()


@pytest.fixture
def manager(register):
  register(Tailscale, SimpleNamespace(ssh=lambda hostname: "unreachable"))
  manager = Manager()
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def workers(db):
  for hostname, subdomain in (("w1", "red"), ("w2", "blue")):
    Worker.create(hostname=hostname, subdomain=subdomain)


@pytest.fixture
def ca(certs, monkeypatch):
  """Let's Encrypt as renewal sees it: each issue writes a certificate for the names asked, each revocation is kept,
  in the order they happen; `fails` makes revoking, or issuing for the names it holds, fail with its message, and an
  issue waits for `hold` when one is set."""
  ca = SimpleNamespace(events=[], fails={}, running=0, most=0, revocations=0, hold=None)
  lock = threading.Lock()

  def issue(certificate):
    with lock:
      ca.running += 1
      ca.most = max(ca.most, ca.running)
    time.sleep(0.01)
    if ca.hold:
      ca.hold.wait(5)
    try:
      if certificate.names in ca.fails:
        raise RuntimeError(ca.fails[certificate.names])
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      certs.reload(certificate)
      ca.events.append(("issued", certificate.names))
    finally:
      with lock:
        ca.running -= 1

  def revoke(certificate, reason):
    ca.revocations += 1
    if "revoke" in ca.fails:
      raise RuntimeError(ca.fails["revoke"])
    ca.events.append(("revoked", tuple(certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))))

  monkeypatch.setattr(certs, "issue", issue)
  monkeypatch.setattr(certs, "acme_client", lambda profile=None: (SimpleNamespace(revoke=revoke), None))
  return ca


def issued(certs, workers=None):
  """Every certificate valid for its names, as after a renewal of `workers` (hostname to subdomain)."""
  certs.set_workers(workers or {"w1": "red", "w2": "blue"})
  for certificate in certs.inventory().values():
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    certs.reload(certificate)


class TestRequest:
  def test_the_renewal_holds_the_certificates_and_shows_in_the_task_history(self, manager):
    manager.request_certificate_renewal()
    manager.request_certificate_renewal()
    assert [(task["key"], task["scopes"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]] == [
        (["renew_certificates"], ["certificate"], False)]

  async def test_a_request_while_one_runs_gets_one_after_it(self, manager, certs, ca, workers):
    ca.hold = threading.Event()
    manager.request_certificate_renewal()
    manager.task_queue.dispatch_tick()
    for _ in range(500):
      if ca.running:
        break
      await asyncio.sleep(0.01)
    manager.request_certificate_renewal()
    snapshot = manager.task_queue.snapshot()
    ca.hold.set()
    assert (len(snapshot["running"]), len(snapshot["queued"])) == (1, 1)
    for _ in range(500):
      if not (manager.task_queue.snapshot()["running"] or manager.task_queue.snapshot()["queued"]):
        break
      manager.task_queue.dispatch_tick()
      await asyncio.sleep(0.01)


class TestRenewal:
  async def test_missing_certificates_are_issued_one_at_a_time_for_the_names_the_rows_give(
          self, manager, certs, ca, workers, notices):
    await manager.renew_certificates()
    assert notices()[0] == ("success", f"Issued the certificate for manager.core.example.test; it expires on "
                            f"{certs.manager.expiry():%Y-%m-%d}.", f"{MANAGER}/cloudflare")
    assert [content.split(";")[0] for _, content, _ in notices()] == [
        f"Issued the certificate for {name}" for name in ("manager.core.example.test", "discovery.int.example.test",
                                                          "*.red.int.example.test", "*.blue.int.example.test")]
    assert ca.events == [("issued", ("manager.core.example.test",)), ("issued", ("discovery.int.example.test",)),
                         ("issued", ("*.red.int.example.test",)), ("issued", ("*.blue.int.example.test",))]
    assert ca.most == 1
    assert all(certificate.is_valid() for certificate in certs.inventory().values())

  async def test_valid_certificates_are_left_and_one_due_is_renewed(self, manager, certs, ca, workers):
    issued(certs)
    write_certificate(certs.workers["w2"].fullchain_path.parent, certs.workers["w2"].names, timedelta(days=3))
    certs.reload(certs.workers["w2"])
    await manager.renew_certificates()
    assert ca.events == [("issued", ("*.blue.int.example.test",))]

  async def test_a_changed_subdomain_revokes_the_old_chain_before_the_new_one_is_issued(self, manager, certs, ca, workers):
    issued(certs)
    Worker.update(subdomain="green").where(Worker.hostname == "w1").execute()
    await manager.renew_certificates()
    assert ca.events == [("revoked", ("*.red.int.example.test",)), ("issued", ("*.green.int.example.test",))]
    assert certs.workers["w1"].covered == {"*.green.int.example.test"}
    assert not certs.workers["w1"].retired_directory.exists()

  async def test_a_removed_workers_certificate_is_revoked_and_its_directory_removed(self, manager, certs, ca, workers):
    issued(certs, {"w1": "red", "w2": "blue", "w3": "green"})
    directory = certs.workers["w3"].fullchain_path.parent
    del certs.workers["w3"]
    await manager.renew_certificates()
    assert ca.events == [("revoked", ("*.green.int.example.test",))]
    assert not directory.exists() and "w3" not in certs.workers

  async def test_a_failed_revocation_holds_back_only_its_own_issue_and_fails_the_task(self, manager, certs, ca, workers):
    issued(certs)
    certs.discovery.fullchain_path.unlink()
    certs.reload(certs.discovery)
    Worker.update(subdomain="green").where(Worker.hostname == "w1").execute()
    ca.fails["revoke"] = "CA offline"
    with pytest.raises(TaskFailed):
      await manager.renew_certificates()
    assert ca.events == [("issued", ("discovery.int.example.test",))]
    assert list(certs.workers["w1"].retired_directory.glob("*.pem"))
    assert "CA offline" in certs.workers["w1"].error
    assert ca.revocations == 1

  async def test_a_failed_issue_keeps_the_current_certificate_lets_the_others_go_on_and_fails_the_task(
          self, manager, certs, ca, workers, notices):
    issued(certs)
    certificate = certs.workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names, timedelta(days=3))
    certs.reload(certificate)
    certs.workers["w2"].fullchain_path.unlink()
    certs.reload(certs.workers["w2"])
    key = certificate.key_path.read_bytes()
    ca.fails[certificate.names] = "order rejected"
    with pytest.raises(TaskFailed):
      await manager.renew_certificates()
    assert certificate.is_valid() and certificate.key_path.read_bytes() == key
    assert notices()[-1] == ("error", "Certificates could not all be renewed: *.red.int.example.test: order rejected", f"{MANAGER}/cloudflare")
    assert certificate.error == "order rejected"
    assert ca.events == [("issued", ("*.blue.int.example.test",))]

  @pytest.mark.parametrize("missing, reloaded", [(True, 1), (False, 0)])
  async def test_the_managers_certificate_is_reloaded_in_place_only_when_it_was_issued(
          self, manager, certs, ca, workers, missing, reloaded):
    issued(certs)
    if missing:
      certs.manager.fullchain_path.unlink()
      certs.reload(certs.manager)
    loads = []
    certs.tls_context = SimpleNamespace(load_cert_chain=lambda *paths: loads.append(paths))
    await manager.renew_certificates()
    assert loads == [(certs.manager.fullchain_path, certs.manager.key_path)] * reloaded
