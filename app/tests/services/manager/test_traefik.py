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
from services.manager._common import routing_input_hash
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

  def request_domain_records_sync(self):
    self.records_requests += 1

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
class TestApplicationRouting:
  def sync(self, tmp_path, application, online=("w1", "w2", "w3")):
    manager = Syncing(tmp_path, online)
    asyncio.run(manager.sync_application_traefik_domains_config(application.id))
    return manager

  def test_a_worker_that_runs_it_actively_routes_the_names_linked_to_its_own_container_and_no_other_worker_any(
          self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    blue = Container.create(application=application, worker=workers[1], status="inactive")
    Domain.create(name="shop", type="public", port=80, container=red)
    Domain.create(name="api", type="internal", port=8080, container=red)
    Domain.create(name="db", type="tcp", port=5432, container=red)
    Domain.create(name="shop-blue", type="public", port=80, container=blue)

    manager = self.sync(tmp_path, application)

    assert rules(manager.tailscale.files("w1")) == {
        "demo-web-public-shop.yml": ["Host(`shop.example.test`)", ping("shop.example.test")],
        "demo-web-internal-api.yml": ["Host(`api.red.int.example.test`)", ping("api.red.int.example.test")],
        "demo-web-tcp-db.yml": ["HostSNI(`db.red.int.example.test`)"],
    }
    assert manager.tailscale.files("w2") == manager.tailscale.files("w3") == {}

  def test_each_worker_routes_only_the_names_linked_to_its_own_container(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    blue = Container.create(application=application, worker=workers[1], status="active")
    Domain.create(name="shop-red", type="public", port=80, container=red)
    Domain.create(name="shop", type="internal", port=80, container=blue)

    manager = self.sync(tmp_path, application)

    assert rules(manager.tailscale.files("w1")) == {
        "demo-web-public-shop-red.yml": ["Host(`shop-red.example.test`)", ping("shop-red.example.test")]}
    assert rules(manager.tailscale.files("w2")) == {
        "demo-web-internal-shop.yml": ["Host(`shop.blue.int.example.test`)", ping("shop.blue.int.example.test")]}

  def test_every_file_it_writes_is_filled_in_routes_to_the_application_on_its_port_and_the_probe_to_traefik(
          self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    Domain.create(name="shop", type="public", port=80, container=red)
    Domain.create(name="api", type="internal", port=8080, container=red)
    Domain.create(name="db", type="tcp", port=5432, container=red)

    files = self.sync(tmp_path, application).tailscale.files("w1")

    assert all("${" not in content for content in files.values())
    loaded = {name: yaml.safe_load(content) for name, content in files.items()}
    public, internal = loaded["demo-web-public-shop.yml"]["http"], loaded["demo-web-internal-api.yml"]["http"]
    assert public["services"]["demo-web_shop_public"]["loadBalancer"]["servers"] == [{"url": "http://demo-web:80"}]
    assert internal["services"]["demo-web_api_internal"]["loadBalancer"]["servers"] == [{"url": "http://demo-web:8080"}]
    assert loaded["demo-web-tcp-db.yml"]["tcp"]["services"]["demo-web_db_tcp"]["loadBalancer"]["servers"] == [
        {"address": "demo-web:5432"}]
    assert public["routers"]["demo-web_shop_public_ping"]["service"] == "ping@internal"
    assert internal["routers"]["demo-web_api_internal_ping"]["middlewares"][0] == "tailscale-only"

  def test_its_stale_files_go_last_and_other_applications_and_the_workers_own_stay(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    Domain.create(name="shop", type="public", port=80, container=red)
    manager = Syncing(tmp_path, ("w1", "w2", "w3"))
    kept = ["config.yml", "certs.yml", "demo-api-public-shop.yml", "demo-webx-public-shop.yml",
            "_discovery-public-shop.yml"]
    for hostname in ("w1", "w2"):
      for name in [*kept, "demo-web-public-old.yml", "demo-web-internal-shop.yml"]:
        manager.tailscale.place(hostname, name)

    asyncio.run(manager.sync_application_traefik_domains_config(application.id))

    assert sorted(manager.tailscale.files("w1")) == sorted([*kept, "demo-web-public-shop.yml"])
    assert sorted(manager.tailscale.files("w2")) == sorted(kept)
    on_w1 = [entry for entry in manager.tailscale.log if entry[0] == "w1" and "/revisions/" not in entry[2]]
    assert on_w1[-1][1] == "run" and "-delete" in on_w1[-1][2]

  def test_an_offline_worker_is_left_as_it_is(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    Domain.create(name="shop", type="public", port=80, container=red)
    manager = Syncing(tmp_path, ("w2",))
    manager.tailscale.place("w1", "demo-web-public-old.yml")

    asyncio.run(manager.sync_application_traefik_domains_config(application.id))

    assert list(manager.tailscale.files("w1")) == ["demo-web-public-old.yml"]
    assert not any(entry[0] == "w1" for entry in manager.tailscale.log)

  def test_each_online_worker_is_stamped_and_the_records_are_asked_for_once_the_files_are_written(
          self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    Domain.create(name="shop", type="public", port=80, container=red)

    manager = self.sync(tmp_path, application, online=("w1", "w2"))

    stamp = routing_input_hash("example.test", list(application.domains), list(application.containers), manager.is_worker_online)
    assert [(manager.tailscale.home(hostname) / "revisions/demo-web").read_text() for hostname in ("w1", "w2")] == [stamp] * 2
    assert manager.records_requests == 1
    assert manager.links == ["https://shop.example.test"]
    assert Application.get_by_id(application.id).domains_synced is True


@pytest.mark.usefixtures("quiet")
class TestDiscovery:
  def test_each_service_lists_the_names_linked_to_a_container_in_it_apart_for_each_namespace(
          self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0])
    blue = Container.create(application=application, worker=workers[1])
    Domain.create(name="shop-red", type="public", port=80, container=red, service="shop", pool="eu")
    Domain.create(name="shop-blue", type="public", port=80, container=blue, service="shop")
    Domain.create(name="shop", type="internal", port=80, container=red, service="shop", pool="eu")
    Domain.create(name="db", type="tcp", port=80, container=blue, service="shop")
    Domain.create(name="alone", type="public", port=80, container=red)
    unlinked = Domain.create(name="gone", type="public", port=80, container=blue, service="shop")
    Domain.update(container=None).where(Domain.id == unlinked.id).execute()

    assert Syncing(tmp_path, ()).discovery_services() == [
        ["internal", "shop", [["db.blue.int.example.test", None], ["shop.red.int.example.test", "eu"]]],
        ["public", "shop", [["shop-blue.example.test", None], ["shop-red.example.test", "eu"]]],
    ]

  def test_every_online_worker_answers_each_service_from_its_own_files_and_is_stamped(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0])
    Domain.create(name="shop-red", type="public", port=80, container=red, service="shop", pool="eu")
    Domain.create(name="shop", type="internal", port=80, container=red, service="api")
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
    domain = Domain.create(name="shop-red", type="public", port=80, container=red, service="shop")
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
    asyncio.run(manager.sync_domain_records())
    return manager.written

  def test_discovery_goes_to_the_platforms_tunnel_and_each_linked_public_name_to_its_containers_workers_tunnel(
          self, application, workers):
    red = Container.create(application=application, worker=workers[0], status="inactive")
    blue = Container.create(application=application, worker=workers[1], status="active")
    Domain.create(name="shop", type="public", port=80, container=red)
    Domain.create(name="api", type="public", port=80, container=blue)
    Domain.create(name="db", type="internal", port=80, container=blue)
    Domain.update(container=None).where(Domain.id == Domain.create(name="gone", type="public", port=80, container=red).id).execute()

    assert self.records()[0] == ({
        "sage-tunnel": {"discovery.example.test": "t0.cfargotunnel.com"},
        "sage-domain": {"shop.example.test": "t1.cfargotunnel.com", "api.example.test": "t2.cfargotunnel.com"},
    }, {"type": "CNAME", "proxied": True})

  def test_a_worker_without_its_tunnel_yet_has_no_record_and_the_others_do(self, application, workers):
    Domain.create(name="shop", type="public", port=80, container=Container.create(application=application, worker=workers[0]))
    Domain.create(name="api", type="public", port=80, container=Container.create(application=application, worker=workers[1]))
    assert self.records({"sage-example.test-w2": "t2"})[0][0]["sage-domain"] == {"api.example.test": "t2.cfargotunnel.com"}

  def test_internal_discovery_goes_round_the_online_workers_and_a_removed_workers_record_goes(self, workers):
    zone = [{"name": "discovery.int.example.test", "content": "100.64.0.9", "comment": "sage-discovery-gone"}]
    assert self.records(zone=zone, online=("w1", "w3"))[1] == ({
        "sage-discovery-gone": {},
        "sage-discovery-w1": {"discovery.int.example.test": "100.64.0.2"},
        "sage-discovery-w2": {},
        "sage-discovery-w3": {"discovery.int.example.test": "100.64.0.4"},
    }, {"type": "A"})


class Reconciling(Syncing):
  read_worker_revisions = WorkersMixin.read_worker_revisions

  def __init__(self, root, online):
    super().__init__(root, online)
    self.resynced = []

  def request_application_traefik_sync(self, application):
    self.resynced.append(application.name)

  def add_task(self, **kwargs):
    self.resynced.append(kwargs["params"]["application_id"])


@pytest.mark.usefixtures("quiet")
class TestReconcile:
  def test_an_application_is_resynced_when_a_worker_that_runs_it_actively_misses_its_files_only(
          self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    blue = Container.create(application=application, worker=workers[1], status="inactive")
    Domain.create(name="shop", type="public", port=80, container=red)
    Domain.create(name="shop-blue", type="public", port=80, container=blue)
    manager = Reconciling(tmp_path, ("w1", "w2"))
    asyncio.run(manager.sync_application_traefik_domains_config(application.id))
    asyncio.run(manager.sync_discovery())

    asyncio.run(manager.reconcile_traefik_configs())
    assert (manager.resynced, manager.discovery_requests) == ([], 0)

    (manager.tailscale.home("w1") / "traefik/dynamic/demo-web-public-shop.yml").unlink()
    asyncio.run(manager.reconcile_traefik_configs())
    assert manager.resynced == ["web"]

  def test_discovery_is_asked_for_when_a_workers_stamp_is_not_what_its_files_would_be(self, tmp_path, application, workers):
    red = Container.create(application=application, worker=workers[0], status="active")
    manager = Reconciling(tmp_path, ("w1",))
    asyncio.run(manager.sync_discovery())
    Domain.create(name="shop", type="public", port=80, container=red, service="shop")

    asyncio.run(manager.reconcile_traefik_configs())

    assert manager.discovery_requests == 1
