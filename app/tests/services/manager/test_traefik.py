import asyncio
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import config
from services.cloudflare import CloudflareError
from services.db import Application, Container, Domain, Worker
from services.manager.traefik import TraefikMixin
from services.manager.workers import WorkersMixin
from services.updates import Updates
from utils.executor import active_executor


class TestSetupDomain:
  """Cloudflare is connected before Sage serves, retrying a cold start for failures that clear by
  themselves; a certificate that still works lets it serve anyway."""

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

  @pytest.mark.parametrize("status", [429, 500, 503])
  def test_a_failure_that_clears_waits_and_tries_again(self, sleeps, caplog, status):
    manager = self.manager([CloudflareError(status, "busy"), None])

    with caplog.at_level(logging.WARNING, logger="services.manager.traefik"):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 2
    assert sleeps == [60]
    assert manager.ensured
    assert f"Cloudflare setup failed during startup: Cloudflare answered HTTP {status}: busy. Retrying in 60s." in caplog.text

  @pytest.mark.parametrize("error", [
      CloudflareError(401, "Invalid API Token"),
      CloudflareError(403, "Forbidden"),
      ValueError("The Cloudflare API token is not active."),
      Exception("zone not found"),
  ])
  def test_a_failure_that_does_not_clear_stops_the_start_at_once(self, sleeps, error):
    manager = self.manager([error])

    with pytest.raises(type(error)):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 1
    assert sleeps == []
    assert not manager.ensured

  def test_it_gives_up_after_the_last_delay_and_never_asks_for_a_certificate(self, sleeps):
    manager = self.manager([CloudflareError(503, "first"), CloudflareError(503, "second"), CloudflareError(503, "last")])

    with pytest.raises(CloudflareError, match="last"):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 3
    assert sleeps == [60, 240]
    assert not manager.ensured

  @pytest.mark.parametrize("error", [CloudflareError(401, "Invalid API Token"), CloudflareError(503, "busy")])
  def test_with_a_certificate_that_still_works_it_serves_at_once(self, sleeps, caplog, error):
    manager = self.manager([error], valid=True)

    with caplog.at_level(logging.ERROR, logger="services.manager.traefik"):
      TraefikMixin.setup_domain(manager)

    assert manager.calls == 1
    assert sleeps == []
    assert not manager.ensured
    assert f"Cloudflare setup failed during startup: {error}. Continuing on the current certificate." in caplog.text


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
        write_worker_revision=write_worker_revision, update_worker=lambda hostname, **inputs: None)

    await TraefikMixin.sync_certificates_to_worker(manager, SimpleNamespace(hostname="w1"))

    assert dict(calls) == {
        "/opt/sage/traefik/certs/fullchain.pem": None,
        "/opt/sage/traefik/certs/key.pem": 0o600,
        "/opt/sage/traefik/dynamic/certs.yml": None,
    }


class LocalWorkers:
  """Workers whose Sage home is a folder each under `root`; their commands run in a local shell, and every file
  written or command run is logged in order."""

  def __init__(self, root):
    self.root = root
    self.log = []

  def home(self, hostname):
    return self.root / hostname

  def files(self, hostname):
    dynamic = self.home(hostname) / "traefik/dynamic"
    return {path.name: path.read_text() for path in dynamic.iterdir()} if dynamic.exists() else {}

  def place(self, hostname, name, content="x: 1\n"):
    dynamic = self.home(hostname) / "traefik/dynamic"
    dynamic.mkdir(parents=True, exist_ok=True)
    (dynamic / name).write_text(content)

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    content = Path(src).read_text()
    for key, value in (keys or {}).items():
      content = content.replace("${" + key + "}", str(value))
    path = self.home(hostname) / Path(dst).relative_to("/opt/sage")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    self.log.append((hostname, "write", dst))

  async def exec_command(self, hostname, command, timeout=300):
    self.home(hostname).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command.replace("/opt/sage", str(self.home(hostname))), shell=True, capture_output=True, text=True)
    self.log.append((hostname, "run", command))
    return result.returncode, result.stdout.splitlines()


class Syncing(TraefikMixin):
  """The routing sync of a manager whose workers are `LocalWorkers`, those in `online` online."""
  worker_home_dir = "/opt/sage"
  write_worker_revision = WorkersMixin.write_worker_revision

  def __init__(self, root, online):
    self.tailscale = LocalWorkers(root)
    self.cloudflare = SimpleNamespace(domain="example.test")
    self.online = set(online)
    self.records_requests = 0
    self.discovery_requests = 0
    self.links = []

  def is_worker_online(self, hostname):
    return hostname in self.online

  def online_workers(self):
    return [worker for worker in Worker.select().order_by(Worker.hostname) if worker.hostname in self.online]

  def notify(self, message, type="info", link=None):
    self.links.append(link)

  async def sync_discovery_records(self):
    pass

  def request_discovery_sync(self):
    self.discovery_requests += 1


def rules(files):
  """Each file's routers' rules."""
  return {name: re.findall(r"rule: (.+)", content) for name, content in files.items()}


def ping(host):
  return f"Host(`{host}`) && Method(`HEAD`) && Path(`/ping`)"


@pytest.fixture
def quiet(register):
  register(Updates, SimpleNamespace(changed=lambda source, key=None: None))


@pytest.fixture
def workers(db):
  return [Worker.create(hostname=hostname, ip=f"100.64.0.{index + 2}", subdomain=subdomain)
          for index, (hostname, subdomain) in enumerate([("w1", "red"), ("w2", "blue"), ("w3", "green")])]


@pytest.mark.usefixtures("quiet")
class TestDiscovery:
  def test_each_service_lists_the_names_linked_to_a_container_in_it_apart_for_each_namespace(
          self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0])
    blue = Container.create(application=application, worker=workers[1])
    Domain.create(name="shop-red", type="public", port=80, container=red, status="synced", service="shop", pool="eu")
    Domain.create(name="shop-blue", type="public", port=80, container=blue, status="synced", service="shop")
    Domain.create(name="shop", type="internal", port=80, container=red, status="synced", service="shop", pool="eu")
    Domain.create(name="db", type="tcp", port=80, container=blue, status="synced", service="shop")
    Domain.create(name="alone", type="public", port=80, container=red, status="synced")
    unlinked = Domain.create(name="gone", type="public", port=80, container=blue, status="synced", service="shop")
    Domain.update(container=None).where(Domain.id == unlinked.id).execute()

    assert Syncing(tmp_path, ()).discovery_services() == [
        ["internal", "shop", [["db.blue.int.example.test", None], ["shop.red.int.example.test", "eu"]]],
        ["public", "shop", [["shop-blue.example.test", None], ["shop-red.example.test", "eu"]]],
    ]

  def test_every_online_worker_answers_each_service_from_its_own_files_and_is_stamped(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0])
    Domain.create(name="shop-red", type="public", port=80, container=red, status="synced", service="shop", pool="eu")
    Domain.create(name="shop", type="internal", port=80, container=red, status="synced", service="api")
    manager = Syncing(tmp_path, ("w1", "w2"))
    for hostname in ("w1", "w2"):
      manager.tailscale.place(hostname, "_discovery-public-old.yml")
      manager.tailscale.place(hostname, "demo-web-public-shop-red.yml")

    asyncio.run(manager.sync_discovery())

    for hostname in ("w1", "w2"):
      files = manager.tailscale.files(hostname)
      assert sorted(files) == ["_discovery-internal-api.yml", "_discovery-public-shop.yml", "demo-web-public-shop-red.yml"]
      public = yaml.safe_load(files["_discovery-public-shop.yml"])["http"]
      assert public["routers"]["discovery_public_shop"]["rule"] == (
          "Host(`discovery.example.test`) && Query(`service`, `shop`) && "
          "(Method(`GET`) || Method(`HEAD`) || Method(`OPTIONS`))")
      headers = public["middlewares"]["discovery_public_shop"]["headers"]
      assert headers["customResponseHeaders"]["Sage-Endpoints"] == '"shop-red.example.test";pool="eu"'
      internal = yaml.safe_load(files["_discovery-internal-api.yml"])["http"]
      assert internal["routers"]["discovery_internal_api"]["rule"].startswith("Host(`discovery.int.example.test`)")
      assert internal["middlewares"]["discovery_internal_api"]["headers"]["customResponseHeaders"]["Sage-Endpoints"] == (
          '"shop.red.int.example.test"')
      assert (manager.tailscale.home(hostname) / "revisions/discovery").read_text() == manager.discovery_hash(
          manager.discovery_services())
    assert manager.tailscale.files("w3") == {}

  def test_the_stamp_follows_the_listed_names(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0])
    domain = Domain.create(name="shop-red", type="public", port=80, container=red, status="synced", service="shop")
    manager = Syncing(tmp_path, ())
    before = manager.discovery_hash(manager.discovery_services())
    domain.pool = "eu"
    domain.save()
    assert manager.discovery_hash(manager.discovery_services()) != before


class DomainRecords(TraefikMixin):
  """The records task of a manager whose Cloudflare has a tunnel for each worker in `tunnels` and a zone of `zone`, and
  whose workers in `online` are online."""

  def __init__(self, tunnels, zone, online):
    self.cloudflare = SimpleNamespace(
        domain="example.test", tunnel_id="t0", worker_tunnel_name=lambda hostname: f"sage-example.test-{hostname}",
        tunnels=lambda: {name: {"id": tunnel_id} for name, tunnel_id in tunnels.items()},
        get_dns_records=lambda: zone, sync_dns_records=self.write)
    self.online = set(online)
    self.written = []

  def is_worker_online(self, hostname):
    return hostname in self.online

  def write(self, records, zone=None, **fields):
    self.written.append((records, fields))


class TestDomainRecords:
  @pytest.fixture(autouse=True)
  def lane(self):
    executor = ThreadPoolExecutor(max_workers=1)
    token = active_executor.set(executor)
    yield
    active_executor.reset(token)
    executor.shutdown()

  def records(self, tunnels=None, zone=(), online=("w1", "w2", "w3")):
    manager = DomainRecords({"sage-example.test-w1": "t1", "sage-example.test-w2": "t2"} if tunnels is None else tunnels,
                            list(zone), online)
    asyncio.run(manager.sync_discovery_records())
    return manager.written

  def test_internal_discovery_goes_round_the_online_workers_and_a_removed_workers_record_goes(self, workers):
    zone = [{"name": "discovery.int.example.test", "content": "100.64.0.9", "comment": "sage-discovery-gone"}]
    assert self.records(zone=zone, online=("w1", "w3"))[1] == ({
        "sage-discovery-gone": {},
        "sage-discovery-w1": {"discovery.int.example.test": "100.64.0.2"},
        "sage-discovery-w2": {},
        "sage-discovery-w3": {"discovery.int.example.test": "100.64.0.4"},
    }, {"type": "A"})
