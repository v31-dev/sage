import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
import yaml

from services.db import Application, Container, Domain, Worker
from services.domains import DomainConfig
from services.manager.core import CoreMixin
from services.manager.domains import DomainsMixin, domain_changed
from services.manager.queue import SCOPES, QueueMixin
from services.updates import Updates, UpdateSource
from tests.services.manager.test_traefik import LocalWorkers
from utils.executor import active_executor
from utils.logging import task_id
from utils.queue import TaskQueue


class Cloudflare:
  def __init__(self):
    self.failure = None
    self.callback = None
    self.missing_tunnel = False

  def connect(self):
    pass

  def worker_tunnel_name(self, hostname):
    return f"sage-example.test-{hostname}"

  def find_tunnel(self, name):
    return None if self.missing_tunnel else {"id": "t1"}

  def sync_dns_records(self, records, **fields):
    if self.callback:
      callback, self.callback = self.callback, None
      callback()
    if self.failure:
      raise self.failure


class Syncing(DomainsMixin, QueueMixin):
  worker_home_dir = "/opt/sage"

  def __init__(self, root, pool):
    self.tailscale = LocalWorkers(root)
    self.cloudflare = Cloudflare()
    self.online = {"w1", "w2"}
    self.status_requests = []
    self.queue_facts = self.domains_queue_facts()
    self.task_queue = TaskQueue(SCOPES, {"app": pool}, notify=self._queue_changed)

  def is_worker_online(self, hostname):
    return hostname in self.online

  def online_workers(self):
    return [worker for worker in Worker.select() if worker.hostname in self.online]

  async def sync_discovery(self, repair=True):
    pass

  def request_application_status_sync(self, application):
    self.status_requests.append(application.id)

  def run(self, domain_id, application_id):
    asyncio.run(self.sync_domain(domain_id, application_id))


@pytest.fixture
def manager(tmp_path, db, thread_connections, register):
  signals = []
  register(Updates, SimpleNamespace(changed=lambda source, key=None: signals.append((source, key))))
  with ThreadPoolExecutor(max_workers=2) as pool:
    token = active_executor.set(pool)
    manager = Syncing(tmp_path, pool)
    manager.signals = signals
    yield manager
    manager.task_queue.stop()
    active_executor.reset(token)


@pytest.fixture
def linked(application, worker):
  Worker.update(subdomain="red").where(Worker.hostname == worker.hostname).execute()
  container = Container.create(application=application, worker=worker, status="active")
  return Domain.create(name="shop", type="public", port=8080, container=container, service="shop")


class TestDomainConfig:
  def test_the_snapshot_is_immutable_and_does_not_follow_later_record_edits(self, linked):
    snapshot = DomainConfig.capture(linked, "example.test", "templates")
    linked.port = 9000
    linked.save()

    assert snapshot.port == 8080
    assert snapshot.content_hash != DomainConfig.capture(linked, "example.test", "templates").content_hash
    with pytest.raises(FrozenInstanceError):
      snapshot.port = 9000

  @pytest.mark.parametrize("field, value", [("name", "store"), ("port", 9000), ("type", "tcp")])
  def test_the_fingerprint_changes_with_rendered_configuration(self, linked, field, value):
    before = DomainConfig.capture(linked, "example.test", "templates")
    setattr(linked, field, value)
    linked.save()

    assert before.content_hash != DomainConfig.capture(linked, "example.test", "templates").content_hash
    assert (before.filename, before.dns_comment) == (
        DomainConfig.capture(linked, "example.test", "templates").filename,
        DomainConfig.capture(linked, "example.test", "templates").dns_comment)

  def test_discovery_metadata_and_sync_results_do_not_change_the_apply_fingerprint(self, linked):
    before = DomainConfig.capture(linked, "example.test", "templates")
    linked.service, linked.pool, linked.status, linked.sync_error = "other", "eu", "error", "DNS: unavailable"
    linked.save()

    assert before.content_hash == DomainConfig.capture(linked, "example.test", "templates").content_hash

  def test_the_fingerprint_includes_the_domain_template_and_container_activity(self, linked):
    before = DomainConfig.capture(linked, "example.test", "templates")
    assert before.content_hash != DomainConfig.capture(linked, "other.test", "templates").content_hash
    assert before.content_hash != DomainConfig.capture(linked, "example.test", "changed").content_hash
    Container.update(status="inactive").where(Container.id == linked.container_id).execute()
    assert before.content_hash != DomainConfig.capture(Domain.get_by_id(linked.id), "example.test", "templates").content_hash


class TestSyncDomain:
  @pytest.mark.parametrize("type", ["public", "internal", "tcp"])
  def test_the_record_routes_to_its_container_and_commits_its_own_result(self, manager, linked, application, clock, type):
    linked.type = type
    linked.save()
    before = Application.get_by_id(application.id).updated_at

    manager.run(linked.id, application.id)

    stored = Domain.get_by_id(linked.id)
    snapshot = manager.domain_config(stored)
    files = manager.tailscale.files("w1")
    assert set(files) == {f"domain-{linked.id}.yml"}
    assert "${" not in files[snapshot.filename]
    rendered = yaml.safe_load(files[snapshot.filename])["tcp" if type == "tcp" else "http"]
    assert rendered["services"][f"domain_{linked.id}"]["loadBalancer"]["servers"] == [
        {"address": "demo-web:8080"} if type == "tcp" else {"url": "http://demo-web:8080"}]
    assert snapshot.host in rendered["routers"][f"domain_{linked.id}"]["rule"]
    if type != "tcp":
      assert rendered["routers"][f"domain_{linked.id}_ping"]["service"] == "ping@internal"
    assert (stored.status, stored.sync_error, stored.synced_hash, stored.synced_at) == (
        "synced", None, snapshot.content_hash, clock.current)
    assert Application.get_by_id(application.id).updated_at == before

  def test_a_stopped_record_removes_only_its_own_route(self, manager, linked):
    manager.tailscale.place("w1", f"domain-{linked.id}.yml")
    manager.tailscale.place("w1", "domain-999.yml")
    Container.update(status="inactive").where(Container.id == linked.container_id).execute()

    manager.run(linked.id, linked.container.application_id)

    assert set(manager.tailscale.files("w1")) == {"domain-999.yml"}
    assert Domain.get_by_id(linked.id).status == "synced"

  def test_an_unlinked_record_removes_reachable_copies_without_waiting_for_an_offline_former_worker(self, manager, linked):
    Worker.create(hostname="w2", ip="100.64.0.3", subdomain="blue")
    for hostname in ("w1", "w2", "offline"):
      manager.tailscale.place(hostname, f"domain-{linked.id}.yml")
    Domain.update(container=None).where(Domain.id == linked.id).execute()

    manager.run(linked.id, None)

    assert manager.tailscale.files("w1") == manager.tailscale.files("w2") == {}
    assert set(manager.tailscale.files("offline")) == {f"domain-{linked.id}.yml"}
    assert Domain.get_by_id(linked.id).status == "synced"

  def test_a_ready_new_target_is_synced_while_an_offline_old_copy_waits_for_cleanup(self, manager, linked, application):
    manager.run(linked.id, application.id)
    blue = Container.create(application=application,
                            worker=Worker.create(hostname="w2", ip="100.64.0.3", subdomain="blue"), status="active")
    linked.container, linked.name = blue, "store"
    linked.save()
    manager.online.remove("w1")

    manager.run(linked.id, application.id)

    assert Domain.get_by_id(linked.id).status == "synced"
    assert "store.example.test" in manager.tailscale.files("w2")[f"domain-{linked.id}.yml"]
    assert "shop.example.test" in manager.tailscale.files("w1")[f"domain-{linked.id}.yml"]

  def test_an_offline_current_target_stays_pending_and_keeps_its_successful_baseline(self, manager, linked, application):
    manager.run(linked.id, application.id)
    before = Domain.get_by_id(linked.id)
    manager.online.clear()
    manager.cloudflare.failure = RuntimeError("unavailable")

    manager.run(linked.id, application.id)

    stored = Domain.get_by_id(linked.id)
    assert (stored.status, stored.sync_error) == ("pending", None)
    assert (stored.synced_hash, stored.synced_at) == (before.synced_hash, before.synced_at)

  @pytest.mark.parametrize("phase", ["Traefik", "DNS"])
  def test_a_failed_apply_names_its_phase_and_retains_the_previous_success(self, manager, linked, application, monkeypatch, phase):
    manager.run(linked.id, application.id)
    before = Domain.get_by_id(linked.id)
    linked.port = 9000
    linked.save()
    if phase == "DNS":
      manager.cloudflare.failure = OSError("unavailable")
    else:
      async def fail(*args, **kwargs):
        raise OSError("unavailable")
      monkeypatch.setattr(manager.tailscale, "sync_file", fail)

    with pytest.raises(OSError, match="unavailable"):
      manager.run(linked.id, application.id)

    stored = Domain.get_by_id(linked.id)
    assert (stored.status, stored.sync_error) == ("error", f"{phase}: unavailable")
    assert (stored.synced_hash, stored.synced_at) == (before.synced_hash, before.synced_at)

  def test_an_edit_during_apply_is_not_certified_by_the_older_attempt(self, manager, linked, application):
    manager.cloudflare.callback = lambda: Domain.update(port=9000).where(Domain.id == linked.id).execute()

    manager.run(linked.id, application.id)

    stored = Domain.get_by_id(linked.id)
    assert (stored.status, stored.synced_hash, stored.synced_at, stored.port) == ("pending", None, None, 9000)
    assert manager.has_task(manager.sync_domain, {"domain_id": linked.id})
    manager.run(linked.id, application.id)
    stored = Domain.get_by_id(linked.id)
    assert (stored.status, stored.synced_hash) == ("synced", manager.domain_config(stored).content_hash)
    assert "9000" in manager.tailscale.files("w1")[f"domain-{linked.id}.yml"]

  def test_a_failure_of_an_older_configuration_does_not_replace_the_new_records_result(self, manager, linked, application):
    manager.cloudflare.callback = lambda: Domain.update(port=9000).where(Domain.id == linked.id).execute()
    manager.cloudflare.failure = OSError("unavailable")

    with pytest.raises(OSError):
      manager.run(linked.id, application.id)

    stored = Domain.get_by_id(linked.id)
    assert (stored.status, stored.sync_error, stored.synced_hash) == ("pending", None, None)
    assert manager.has_task(manager.sync_domain, {"domain_id": linked.id})

  def test_a_relink_requeues_under_the_new_app_without_writing_under_the_old_exclusion(self, manager, linked, project):
    before = linked.container.application_id
    other = Application.create(project=project, name="api", label="API", image="nginx")
    Domain.update(container=Container.create(application=other, worker="w1", status="active")).where(Domain.id == linked.id).execute()

    manager.run(linked.id, before)

    assert manager.tailscale.files("w1") == {}
    (task,) = manager.task_snapshot()["queued"]
    assert set(task["scopes"]) == {f"domain:{linked.id}", "app:demo-api"}
    assert task["params"] == {"domain_id": linked.id, "application_id": other.id}

  def test_a_record_deleted_before_its_task_runs_is_a_quiet_noop(self, manager, linked, application):
    linked.delete_instance()

    manager.run(linked.id, application.id)

    assert Domain.select().count() == 0
    assert manager.tailscale.files("w1") == {}

  def test_a_record_deleted_during_apply_is_not_recreated_by_the_result_write(self, manager, linked, application):
    manager.cloudflare.callback = lambda: Domain.delete().where(Domain.id == linked.id).execute()

    manager.run(linked.id, application.id)

    assert Domain.select().count() == 0

  def test_an_interrupted_busy_container_requests_status_reconciliation(self, manager, linked, application):
    Container.update(status="deploying").where(Container.id == linked.container_id).execute()

    manager.run(linked.id, application.id)

    assert manager.status_requests == [application.id]
    assert manager.tailscale.files("w1") == {}
    assert Domain.get_by_id(linked.id).status == "pending"


class TestDomainQueueState:
  def test_replacing_one_records_request_retains_other_records_of_the_same_app(self, manager, linked, application):
    other = Domain.create(name="other", type="public", port=80, container=linked.container)
    before = Application.get_by_id(application.id).updated_at
    for domain in (linked, other, linked):
      manager.request_domain_sync(domain)

    assert [task["params"]["domain_id"] for task in manager.task_snapshot()["queued"]] == [other.id, linked.id]
    assert Application.get_by_id(application.id).updated_at == before

  def test_syncing_is_derived_from_queued_work_and_not_stored_on_the_record(self, manager, linked):
    manager.request_domain_sync(linked)

    assert Domain.get_by_id(linked.id).status == "pending"
    assert manager.domain_status(linked) == "syncing"
    manager.task_queue.stop()
    assert manager.domain_status(Domain.get_by_id(linked.id)) == "pending"

  def test_queue_completion_notifies_the_domain_and_application_views(self, manager, linked):
    manager.request_domain_sync(linked)
    expected_id = manager.task_snapshot()["queued"][0]["task_id"]
    context = []
    manager.cloudflare.callback = lambda: context.append(task_id.get())
    manager.signals.clear()

    async def drain():
      while manager.task_snapshot()["queued"] or manager.task_snapshot()["running"]:
        manager.dispatch_tick()
        await asyncio.sleep(0)
    asyncio.run(drain())

    assert manager.domain_status(Domain.get_by_id(linked.id)) == "synced"
    assert (UpdateSource.DOMAINS, None) in manager.signals
    assert (UpdateSource.APPLICATION, "demo/web") in manager.signals
    assert context == [expected_id]

  def test_cancelling_queued_work_publishes_the_persisted_state_again(self, manager, linked):
    manager.request_domain_sync(linked)
    manager.signals.clear()

    manager.task_queue.stop()

    assert manager.domain_status(Domain.get_by_id(linked.id)) == "pending"
    assert sorted(manager.signals, key=str) == sorted([
        (UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web")], key=str)

  def test_relinking_notifies_both_application_views(self, manager, linked, project):
    before = linked.container
    other = Application.create(project=project, name="api", label="API", image="nginx")
    linked.container = Container.create(application=other, worker="w1", status="active")
    linked.save()
    manager.signals.clear()

    domain_changed(linked, before)

    assert set(manager.signals) == {
        (UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"), (UpdateSource.APPLICATION, "demo/api")}


def test_summary_counts_domain_sync_results_independently_of_container_health(application, worker):
  inactive = Container.create(application=application, worker=worker, status="inactive")
  Domain.create(name="ready", type="public", port=80, container=inactive, status="synced")
  Domain.create(name="failed", type="public", port=80, container=inactive, status="error")
  Domain.create(name="unlinked", type="public", port=80)

  class Summary(CoreMixin):
    def worker_counts(self):
      return {"total": 1, "online": 1}

  summary = Summary().get_system_summary()

  assert (summary["domains_total"], summary["domains_active"], summary["domains_inactive"]) == (3, 1, 2)
