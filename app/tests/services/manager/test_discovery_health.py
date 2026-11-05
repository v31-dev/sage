import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from peewee import IntegrityError

import config
from services.base import Singleton
from services.db import Container, Database, Domain, Worker, db
from services.manager.application import ApplicationMixin
from services.manager.backups import BackupsMixin
from services.manager.domains import DomainsMixin
from services.manager.queue import SCOPES, QueueMixin
from services.updates import Updates
from services.worker_reports import WorkerReports
from tests.services.manager.test_decisions import report
from tests.services.manager.test_domain_reconciliation import Reconciling, applied
from tests.services.manager.test_worker_list import Workers
from utils.executor import active_executor
from utils.queue import TaskQueue


class Publishing(ApplicationMixin, BackupsMixin, Reconciling):
  pass


@pytest.fixture
def manager(tmp_path, db, thread_connections, register):
  register(Updates, SimpleNamespace(changed=lambda *args: None))
  Worker.create(hostname="w1", ip="100.64.0.2", subdomain="red")
  Worker.create(hostname="w2", ip="100.64.0.3", subdomain="blue")
  with ThreadPoolExecutor(max_workers=2) as pool:
    token = active_executor.set(pool)
    manager = Publishing(tmp_path, pool)
    yield manager
    manager.task_queue.stop()
    active_executor.reset(token)


def endpoints(manager, namespace="internal", service="shop", worker="w1"):
  content = manager.tailscale.files(worker)[f"_discovery-{namespace}-{service}.yml"]
  return yaml.safe_load(content)["http"]["middlewares"][f"discovery_{namespace}_{service}"]["headers"]


def members(manager):
  return [member for _, _, names in manager.discovery_services() for member in names]


def parents(manager):
  return [task for task in manager.task_snapshot()["queued"] if task["name"] == "sync_discovery"]


async def drain(manager):
  for _ in range(1000):
    if not manager.task_snapshot()["queued"] and not manager.task_snapshot()["running"]:
      return
    manager.dispatch_tick()
    await asyncio.sleep(0.001)
  pytest.fail("Discovery work did not settle")


class TestAvailability:
  @pytest.mark.parametrize("status", ["active", "inactive", "error", "deploying", "stopping", "backup", "restoring"])
  @pytest.mark.parametrize("ready", [True, False])
  def test_online_requires_an_active_container_and_cached_worker_readiness(self, manager, application, status, ready):
    domain = applied(manager, application)
    Container.update(status=status).where(Container.id == domain.container_id).execute()
    if not ready:
      manager.not_ready.add("w1")

    assert members(manager) == [["shop.red.int.example.test", None, ready and status == "active"]]

  @pytest.mark.parametrize("status", ["pending", "error"])
  def test_unsynced_records_keep_their_pool_and_are_offline(self, manager, application, status):
    domain = applied(manager, application)
    Domain.update(status=status, pool="eu").where(Domain.id == domain.id).execute()

    assert members(manager) == [["shop.red.int.example.test", "eu", False]]

  def test_unlinked_records_remain_discoverable_with_their_name_and_pool(self, manager, application):
    domain = applied(manager, application)
    Domain.update(container=None, pool="eu").where(Domain.id == domain.id).execute()

    assert members(manager) == [["shop.red.int.example.test", "eu", False]]

  def test_an_active_container_on_an_offline_worker_is_not_advertised_online(self, manager, application):
    domain = applied(manager, application)
    manager.online.remove("w1")

    assert Container.get_by_id(domain.container_id).status == "active"
    assert members(manager) == [["shop.red.int.example.test", None, False]]

  @pytest.mark.parametrize("proof", [None, "stale"])
  def test_a_synced_label_without_current_proof_cannot_advertise_online(self, manager, application, proof):
    domain = applied(manager, application)
    Domain.update(synced_hash=proof).where(Domain.id == domain.id).execute()

    assert members(manager)[0][2] is False

  def test_a_new_active_configuration_waits_for_its_domain_apply(self, manager, application):
    domain = applied(manager, application, status="inactive")
    Container.update(status="active").where(Container.id == domain.container_id).execute()

    assert Domain.get_by_id(domain.id).status == "synced"
    assert members(manager)[0][2] is False
    manager.run(domain.id, application.id)
    assert members(manager)[0][2] is True

  def test_online_is_independent_of_the_applications_rollup(self, manager, application):
    applied(manager, application)
    application.status = "error"
    application.save()

    assert members(manager)[0][2] is True

  def test_a_domain_without_a_service_is_not_a_service_member(self, manager, application):
    domain = applied(manager, application)
    Domain.update(service=None).where(Domain.id == domain.id).execute()

    assert members(manager) == []


class TestPublishedHeaders:
  @pytest.mark.parametrize("type", ["public", "internal", "tcp"])
  @pytest.mark.parametrize("pool", [None, "eu"])
  @pytest.mark.parametrize("ready", [True, False])
  def test_the_header_serializes_online_and_pool_parameters(self, manager, application, type, pool, ready):
    domain = applied(manager, application, type=type)
    Domain.update(pool=pool).where(Domain.id == domain.id).execute()
    if not ready:
      manager.not_ready.add("w1")

    manager.reconcile(repair=False)

    headers = endpoints(manager, "public" if type == "public" else "internal")
    assert headers["customResponseHeaders"]["Sage-Endpoints"] == (
        f'"{domain.host("example.test")}"' + (f';pool="{pool}"' if pool else "")
        + (";online" if ready else ";online=?0"))
    assert headers["customResponseHeaders"]["Cache-Control"] == "private, max-age=30"
    assert headers["accessControlMaxAge"] == 300

  def test_a_service_with_only_offline_members_still_answers(self, manager, application):
    first = applied(manager, application)
    second = applied(manager, application, name="other", worker="w2")
    Domain.update(status="error", pool="eu").where(Domain.id == first.id).execute()
    Domain.update(container=None, pool="us").where(Domain.id == second.id).execute()

    manager.reconcile(repair=False)

    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"] == (
        '"other.blue.int.example.test";pool="us";online=?0, "shop.red.int.example.test";pool="eu";online=?0')

  def test_a_health_flip_changes_the_stamp_without_changing_membership(self, manager, application):
    applied(manager, application)
    manager.reconcile(repair=False)
    before = (manager.tailscale.home("w1") / "revisions/discovery").read_text()
    manager.not_ready.add("w1")

    manager.reconcile(repair=False)

    assert (manager.tailscale.home("w1") / "revisions/discovery").read_text() != before
    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online=?0")

  def test_the_cache_policy_is_part_of_the_generated_configuration(self, manager, application, monkeypatch):
    applied(manager, application)
    manager.reconcile(repair=False)
    before = manager.discovery_hash(manager.discovery_services())
    monkeypatch.setattr(config, "DISCOVERY_CACHE_SECONDS", 60)

    manager.reconcile(repair=False)

    assert manager.discovery_hash(manager.discovery_services()) != before
    assert endpoints(manager)["customResponseHeaders"]["Cache-Control"] == "private, max-age=60"

  def test_generation_uses_worker_readiness_after_slow_inventory_reads(self, manager, application, monkeypatch):
    applied(manager, application)

    def inventory():
      manager.not_ready.add("w1")
      return []
    monkeypatch.setattr(manager.cloudflare, "get_dns_records", inventory)

    manager.reconcile(repair=False)

    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online=?0")


class TestContainerWrites:
  @pytest.mark.parametrize("before, after, changed", [
      ("active", "inactive", True), ("inactive", "active", True), ("active", "error", True),
      ("active", "deploying", True), ("active", "backup", True), ("active", "restoring", True),
      ("active", "stopping", True), ("active", "active", False), ("inactive", "error", False),
      ("error", "deploying", False), ("backup", "restoring", False), ("error", "error", False),
  ])
  def test_only_a_committed_active_boundary_requests_publication(self, manager, application, before, after, changed):
    domain = applied(manager, application, status=before)
    manager.request_discovery_sync(repair=False)
    pending = parents(manager)[0]["task_id"]
    container = Container.get_by_id(domain.container_id)
    container.status = after

    manager.save_container(container)

    assert Container.get_by_id(container.id).status == after
    assert (parents(manager)[0]["task_id"] != pending) is changed
    assert parents(manager)[0]["params"] == {"repair": changed}

  def test_a_failed_status_write_requests_no_publication(self, manager, application):
    with pytest.raises(IntegrityError):
      manager.save_container(Container(application=application, worker="missing", status="active"))
    assert parents(manager) == []

  def test_backup_snapshot_restoration_uses_the_same_publication_boundary(self, manager, application):
    domain = applied(manager, application)
    snapshot = manager._get_application_backup_snapshot(application)
    container = Container.get_by_id(domain.container_id)
    container.status = "backup"
    manager.save_container(container)
    asyncio.run(drain(manager))
    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online=?0")

    manager._restore_application_backup_snapshot(application, snapshot)
    asyncio.run(drain(manager))

    assert Container.get_by_id(container.id).status == "active"
    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online")

  def test_rapid_state_changes_publish_the_latest_db_value(self, manager, application):
    domain = applied(manager, application)
    container = Container.get_by_id(domain.container_id)
    for status in ["inactive", "active", "inactive", "active"]:
      container.status = status
      manager.save_container(container)
    assert len(parents(manager)) == 1

    asyncio.run(drain(manager))

    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online")

  def test_a_change_during_an_inflight_parent_is_retained(self, manager, application):
    domain = applied(manager, application)
    manager.request_discovery_sync()

    def stopped():
      assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online")
      container = Container.get_by_id(domain.container_id)
      container.status = "inactive"
      manager.save_container(container)
    manager.cloudflare.callback = stopped

    asyncio.run(drain(manager))

    assert Container.get_by_id(domain.container_id).status == "inactive"
    assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online=?0")

  def test_discovery_can_publish_offline_while_an_app_operation_holds_its_scope(self, manager, application):
    domain = applied(manager, application)

    async def scenario():
      finish = asyncio.Event()

      async def deploying():
        container = Container.get_by_id(domain.container_id)
        container.status = "deploying"
        manager.save_container(container)
        await finish.wait()
        container.status = "active"
        manager.save_container(container)

      manager.add_task(task=deploying, scopes={"app:demo-web"}, executor="app", quiet=True)
      try:
        for _ in range(1000):
          manager.dispatch_tick()
          await asyncio.sleep(0.001)
          if "_discovery-internal-shop.yml" in manager.tailscale.files("w1"):
            break
        else:
          pytest.fail("Discovery was blocked by the application scope")
        assert any(task["name"] == "deploying" for task in manager.task_snapshot()["running"])
        assert endpoints(manager)["customResponseHeaders"]["Sage-Endpoints"].endswith(";online=?0")
      finally:
        finish.set()
      await drain(manager)
    asyncio.run(scenario())


class Monitoring(Workers, QueueMixin, DomainsMixin):
  add_task = QueueMixin.add_task
  request_discovery_sync = DomainsMixin.request_discovery_sync

  def __init__(self, pool):
    super().__init__()
    self.queue_facts = self.workers_queue_facts()
    self.task_queue = TaskQueue(SCOPES, {"app": pool}, notify=self._queue_changed)

  async def sync_discovery(self, repair=True):
    pass

  def notify(self, *args):
    pass


@pytest.fixture
def monitoring(db, clock, register):
  register(Updates, SimpleNamespace(changed=lambda *args: None))
  WorkerReports()
  with ThreadPoolExecutor(max_workers=1) as pool:
    manager = Monitoring(pool)
    yield manager
    manager.task_queue.stop()


async def worker_action(worker_hostname):
  pass


def worker_queue_callbacks(root, reply):
  try:
    config.DATA_DIR = Path(root)
    config.DB_PATH = config.DATA_DIR / "queue.db"
    Database()
    Singleton._instances[Updates] = SimpleNamespace(changed=lambda *args: None)
    WorkerReports()
    with ThreadPoolExecutor(max_workers=1) as pool:
      manager = Monitoring(pool)
      manager.add("w1", True)
      manager.add_task(task=worker_action, name="reboot_worker", scopes={"platform", "app", "domain"}, executor="app",
                       params={"worker_hostname": "w1"})
      manager.update_workers({})
      reply.send((manager.worker_status("w1"), len(parents(manager))))
      manager.task_queue.stop()
  except BaseException as error:
    reply.send(repr(error))
  finally:
    if not db.is_closed():
      db.close()
    reply.close()


class TestWorkerReadiness:
  def test_a_not_ready_report_requests_publication_without_changing_connectivity(self, monitoring, clock):
    monitoring.add("w1", True)
    asyncio.run(drain(monitoring))

    monitoring.update_worker("w1", report=report(traefik_reachable=False))

    assert monitoring.worker_online("w1") is True
    assert monitoring.worker_status("w1") == "not ready"
    assert len(parents(monitoring)) == 1
    assert parents(monitoring)[0]["params"] == {"repair": True}

  def test_stale_reports_and_recovery_request_new_publications(self, monitoring, clock):
    monitoring.add("w1", True)
    monitoring.update_worker("w1", report=report())
    asyncio.run(drain(monitoring))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))

    monitoring.update_workers({})

    assert monitoring.worker_status("w1") == "not ready"
    assert len(parents(monitoring)) == 1
    asyncio.run(drain(monitoring))
    monitoring.update_worker("w1", report=report(arrived_at=clock.current))
    assert monitoring.worker_status("w1") == "online"
    assert len(parents(monitoring)) == 1

  def test_startup_grace_expiry_requests_publication_on_the_existing_refresh(self, monitoring, clock):
    monitoring.add("w1", True)
    assert monitoring.ready_workers() == frozenset({"w1"})
    asyncio.run(drain(monitoring))
    clock.advance(timedelta(seconds=config.REPORT_STARTUP_GRACE_SECONDS))

    monitoring.update_workers({})

    assert monitoring.worker_online("w1") is True
    assert monitoring.worker_status("w1") == "not ready"
    assert len(parents(monitoring)) == 1

  def test_an_unchanged_ready_state_does_not_queue_for_reports_or_paths(self, monitoring):
    monitoring.add("w1", True)
    monitoring.update_worker("w1", report=report())
    asyncio.run(drain(monitoring))

    for _ in range(3):
      monitoring.update_worker("w1", report=report(), path="direct")
    assert parents(monitoring) == []

  def test_readiness_changes_coalesce_to_one_queued_pass(self, monitoring):
    monitoring.add("w1", True)
    for ready in [False, True, False, True]:
      monitoring.update_worker("w1", report=report(traefik_reachable=ready))

    assert monitoring.ready_workers() == frozenset({"w1"})
    assert len(parents(monitoring)) == 1

  def test_offlining_an_already_not_ready_worker_still_updates_discovery_dns(self, monitoring):
    monitoring.add("w1", True)
    monitoring.update_worker("w1", report=report(traefik_reachable=False))
    asyncio.run(drain(monitoring))

    monitoring.set_worker_offline(Worker.get_by_id("w1"))

    assert monitoring.worker_online("w1") is False
    assert len(parents(monitoring)) == 1

  def test_worker_removal_forgets_its_requested_ready_projection(self, monitoring):
    monitoring.add("w1", True)
    asyncio.run(drain(monitoring))

    monitoring._forget_worker("w1")

    assert monitoring.ready_workers() == frozenset()
    assert monitoring.worker_discovery.last == frozenset()
    assert len(parents(monitoring)) == 1

  def test_queue_fact_callbacks_do_not_reenter_their_own_lock(self, tmp_path):
    context = multiprocessing.get_context("spawn")
    reply, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker_queue_callbacks, args=(str(tmp_path), sender))
    process.start()
    sender.close()
    try:
      process.join(5)
      assert not process.is_alive(), "Worker queue-fact callback deadlocked"
      assert process.exitcode == 0
      assert reply.poll(0.1)
      assert reply.recv() == ("rebooting", 1)
    finally:
      if process.is_alive():
        process.terminate()
        process.join(1)
      process.close()
      reply.close()
