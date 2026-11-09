import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from services.cloudflare import dns_record_matches
from services.db import Application, Container, Domain, Worker
from services.manager.domains import DomainsMixin
from services.manager.workers import WorkersMixin
from services.updates import Updates, UpdateSource
from tests.services.manager.test_domains import Cloudflare as LeafCloudflare
from tests.services.manager.test_domains import Syncing
from utils.executor import active_executor


class Cloudflare(LeafCloudflare):
  domain = "example.test"
  tunnel_id = "t0"

  def __init__(self):
    super().__init__()
    self.zone = []
    self.zone_error = None
    self.tunnel_data = {"sage-example.test-w1": {"id": "t1"}, "sage-example.test-w2": {"id": "t2"}}

  def get_dns_records(self):
    if self.zone_error:
      raise self.zone_error
    return self.zone.copy()

  def tunnels(self):
    return self.tunnel_data.copy()

  def find_tunnel(self, name):
    return self.tunnel_data.get(name)


class Reconciling(Syncing):
  sync_discovery = DomainsMixin.sync_discovery
  read_worker_revisions = WorkersMixin.read_worker_revisions
  write_worker_revision = WorkersMixin.write_worker_revision

  def __init__(self, root, pool):
    super().__init__(root, pool)
    self.cloudflare = Cloudflare()

  def reconcile(self, repair=True):
    asyncio.run(self.sync_discovery(repair=repair))


@pytest.fixture
def manager(tmp_path, db, thread_connections, register):
  signals = []
  register(Updates, SimpleNamespace(changed=lambda source, key=None: signals.append((source, key))))
  Worker.create(hostname="w1", ip="100.64.0.2", subdomain="red")
  Worker.create(hostname="w2", ip="100.64.0.3", subdomain="blue")
  with ThreadPoolExecutor(max_workers=2) as pool:
    token = active_executor.set(pool)
    manager = Reconciling(tmp_path, pool)
    manager.signals = signals
    yield manager
    manager.task_queue.stop()
    active_executor.reset(token)


def applied(manager, application, *, worker="w1", name="shop", type="internal", status="active"):
  container, _ = Container.get_or_create(application=application, worker=worker, defaults={"status": status})
  domain = Domain.create(name=name, type=type, port=80, container=container, service="shop")
  manager.run(domain.id, application.id)
  if type == "public":
    manager.cloudflare.zone.append({"name": f"{name}.example.test", "content": f"t{worker[-1]}.cfargotunnel.com",
                                    "comment": f"sage-domain-{domain.id}", "type": "CNAME", "proxied": True})
  return Domain.get_by_id(domain.id)


def repairs(manager):
  return [task["params"]["domain_id"] for task in manager.task_snapshot()["queued"] if task["name"] == "sync_domain"]


class TestRecordRepair:
  def test_an_exact_missing_file_repairs_only_its_record(self, manager, application):
    missing = applied(manager, application)
    other = applied(manager, application, name="other")
    (manager.tailscale.home("w1") / f"traefik/dynamic/domain-{missing.id}.yml").unlink()
    manager.signals.clear()

    manager.reconcile()

    assert repairs(manager) == [missing.id]
    assert Domain.get_by_id(missing.id).status == "pending"
    assert Domain.get_by_id(other.id).status == "synced"
    assert (UpdateSource.DOMAINS, None) in manager.signals
    assert (UpdateSource.APPLICATION, "demo/web") in manager.signals
    discovery = manager.tailscale.files("w1")["_discovery-internal-shop.yml"]
    assert '"other.red.int.example.test";online' in discovery
    assert '"shop.red.int.example.test";online=?0' in discovery

  @pytest.mark.parametrize("status", ["pending", "error"])
  def test_unfinished_work_is_retried_without_losing_its_last_success(self, manager, application, status):
    domain = applied(manager, application)
    Domain.update(status=status, sync_error="DNS: unavailable").where(Domain.id == domain.id).execute()

    manager.reconcile()

    stored = Domain.get_by_id(domain.id)
    assert repairs(manager) == [domain.id]
    assert (stored.status, stored.sync_error, stored.synced_hash, stored.synced_at) == (
        "pending", None, domain.synced_hash, domain.synced_at)

  def test_a_stale_success_fingerprint_retries_the_record(self, manager, application):
    domain = applied(manager, application)
    Domain.update(port=9000).where(Domain.id == domain.id).execute()

    manager.reconcile()

    assert repairs(manager) == [domain.id]
    assert Domain.get_by_id(domain.id).synced_hash == domain.synced_hash

  @pytest.mark.parametrize("change", ["missing", "content", "name", "type", "proxied", "extra"])
  def test_dns_drift_invalidates_the_owned_record(self, manager, application, change):
    domain = applied(manager, application, type="public")
    if change == "missing":
      manager.cloudflare.zone.clear()
    elif change == "extra":
      manager.cloudflare.zone.append({**manager.cloudflare.zone[0], "name": "extra.example.test"})
    else:
      manager.cloudflare.zone[0][change] = {"content": "wrong", "name": "wrong.example.test", "type": "A", "proxied": False}[change]

    manager.reconcile()

    assert repairs(manager) == [domain.id]
    assert Domain.get_by_id(domain.id).status == "pending"
    assert '"shop.example.test";online=?0' in manager.tailscale.files("w1")["_discovery-public-shop.yml"]

  def test_the_matching_dns_configuration_needs_no_record_repair(self, manager, application):
    domain = applied(manager, application, type="public")

    manager.reconcile()

    assert repairs(manager) == []
    assert Domain.get_by_id(domain.id).status == "synced"

  @pytest.mark.parametrize("owner", ["container", "application"])
  def test_transient_busy_status_does_not_invalidate_proof_or_reap_a_possible_working_copy(self, manager, application, owner):
    domain = applied(manager, application)
    manager.tailscale.place("w2", f"domain-{domain.id}.yml")
    if owner == "container":
      Container.update(status="deploying").where(Container.id == domain.container_id).execute()
    else:
      Application.update(status="restoring").where(Application.id == application.id).execute()

    manager.reconcile()

    assert repairs(manager) == []
    assert Domain.get_by_id(domain.id).status == "synced"
    assert f"domain-{domain.id}.yml" in manager.tailscale.files("w2")

  def test_a_stopped_target_with_a_route_requests_its_record_operation(self, manager, application):
    domain = applied(manager, application, status="inactive")
    manager.tailscale.place("w1", f"domain-{domain.id}.yml")

    manager.reconcile()

    assert repairs(manager) == [domain.id]
    assert f"domain-{domain.id}.yml" in manager.tailscale.files("w1")


class TestCleanup:
  def test_only_exact_orphan_file_identities_are_reaped(self, manager):
    for name in ["domain-42.yml", "config.yml", "certs.yml", "domain-042.yml", "their-domain-42.yml"]:
      manager.tailscale.place("w1", name)

    manager.reconcile()

    assert set(manager.tailscale.files("w1")) == {"config.yml", "certs.yml", "domain-042.yml", "their-domain-42.yml"}

  def test_an_owner_created_during_inventory_is_visible_to_orphan_cleanup(self, manager, application, monkeypatch):
    manager.tailscale.place("w1", "domain-42.yml")
    original = manager.tailscale.exec_command
    created = False

    async def listing(hostname, command, **kwargs):
      nonlocal created
      result = await original(hostname, command, **kwargs)
      if not created and "-printf" in command:
        created = True
        container = Container.create(application=application, worker="w1", status="active")
        Domain.create(id=42, name="shop", type="internal", port=80, container=container)
      return result
    monkeypatch.setattr(manager.tailscale, "exec_command", listing)

    manager.reconcile(repair=False)

    assert Domain.get_by_id(42).status == "pending"
    assert "domain-42.yml" in manager.tailscale.files("w1")

  @pytest.mark.parametrize("status", ["pending", "error"])
  def test_a_failed_or_pending_move_keeps_the_previous_workers_copy(self, manager, application, status):
    domain = applied(manager, application)
    target = Container.create(application=application, worker="w2", status="active")
    Domain.update(container=target, worker="w2", status=status).where(Domain.id == domain.id).execute()

    manager.reconcile()

    assert repairs(manager) == [domain.id]
    assert f"domain-{domain.id}.yml" in manager.tailscale.files("w1")

  def test_a_missing_replacement_keeps_the_old_copy_until_record_repair(self, manager, application):
    domain = applied(manager, application, worker="w2")
    (manager.tailscale.home("w2") / f"traefik/dynamic/domain-{domain.id}.yml").unlink()
    manager.tailscale.place("w1", f"domain-{domain.id}.yml")

    manager.reconcile()

    assert repairs(manager) == [domain.id]
    assert f"domain-{domain.id}.yml" in manager.tailscale.files("w1")

  def test_an_offline_former_workers_artifact_is_reaped_when_it_returns(self, manager, application):
    domain = applied(manager, application)
    target = Container.create(application=application, worker="w2", status="active")
    Domain.update(container=target, worker="w2", status="pending").where(Domain.id == domain.id).execute()
    manager.online.remove("w1")
    manager.run(domain.id, application.id)
    manager.reconcile(repair=False)
    assert f"domain-{domain.id}.yml" in manager.tailscale.files("w1")
    assert Domain.get_by_id(domain.id).status == "synced"

    manager.online.add("w1")
    manager.reconcile()

    assert f"domain-{domain.id}.yml" not in manager.tailscale.files("w1")
    assert f"domain-{domain.id}.yml" in manager.tailscale.files("w2")
    assert repairs(manager) == []

  def test_a_synced_unlinked_record_has_no_files_on_returning_workers(self, manager, application):
    domain = applied(manager, application)
    manager.online.remove("w1")
    Domain.update(container=None, status="pending").where(Domain.id == domain.id).execute()
    manager.run(domain.id, None)
    manager.online.add("w1")

    manager.reconcile()

    assert Domain.get_by_id(domain.id).status == "synced"
    assert f"domain-{domain.id}.yml" not in manager.tailscale.files("w1")
    assert repairs(manager) == []


class TestDiscovery:
  def test_matching_files_and_stamp_are_not_rewritten(self, manager, application):
    applied(manager, application)
    manager.reconcile()
    paths = [path for path in manager.tailscale.root.rglob("*") if path.is_file()]
    before = {path: path.stat().st_mtime_ns for path in paths}

    manager.reconcile()

    assert {path: path.stat().st_mtime_ns for path in paths} == before
    assert repairs(manager) == []

  def test_a_matching_stamp_cannot_hide_a_missing_discovery_file(self, manager, application):
    applied(manager, application)
    manager.reconcile()
    path = manager.tailscale.home("w1") / "traefik/dynamic/_discovery-internal-shop.yml"
    path.unlink()

    manager.reconcile()

    assert "shop.red.int.example.test" in path.read_text()

  def test_removed_members_are_rewritten_and_obsolete_service_files_are_removed(self, manager, application):
    domain = applied(manager, application)
    manager.reconcile()
    Domain.update(service="new").where(Domain.id == domain.id).execute()

    manager.reconcile()

    files = manager.tailscale.files("w1")
    assert "_discovery-internal-shop.yml" not in files
    assert "shop.red.int.example.test" in files["_discovery-internal-new.yml"]

  def test_one_workers_failed_discovery_write_does_not_poison_domain_proof_or_other_workers(self, manager, application, monkeypatch):
    domain = applied(manager, application)
    original = manager.tailscale.sync_file

    async def write(hostname, source, destination, *args, **kwargs):
      if hostname == "w1" and "_discovery-" in destination:
        raise OSError("unavailable")
      return await original(hostname, source, destination, *args, **kwargs)
    monkeypatch.setattr(manager.tailscale, "sync_file", write)

    with pytest.raises(RuntimeError, match="w1 cleanup/discovery: unavailable"):
      manager.reconcile()

    stored = Domain.get_by_id(domain.id)
    assert (stored.status, stored.synced_hash, stored.synced_at) == ("synced", domain.synced_hash, domain.synced_at)
    assert "_discovery-internal-shop.yml" in manager.tailscale.files("w2")
    assert not (manager.tailscale.home("w1") / "revisions/discovery").exists()

  def test_an_unknown_file_inventory_is_not_misread_as_missing_artifacts(self, manager, application, monkeypatch):
    first = applied(manager, application)
    second = applied(manager, application, worker="w2")
    (manager.tailscale.home("w2") / f"traefik/dynamic/domain-{second.id}.yml").unlink()
    original = manager.tailscale.exec_command

    async def listing(hostname, command, **kwargs):
      if hostname == "w1" and "-printf" in command:
        raise OSError("unavailable")
      return await original(hostname, command, **kwargs)
    monkeypatch.setattr(manager.tailscale, "exec_command", listing)

    with pytest.raises(RuntimeError, match="w1 files: unavailable"):
      manager.reconcile()

    assert repairs(manager) == [second.id]
    assert Domain.get_by_id(first.id).status == "synced"

  def test_a_failed_dns_inventory_still_allows_local_orphan_cleanup(self, manager):
    manager.tailscale.place("w1", "domain-42.yml")
    manager.cloudflare.zone_error = OSError("unavailable")

    with pytest.raises(RuntimeError, match="DNS inventory: unavailable"):
      manager.reconcile()

    assert manager.tailscale.files("w1") == {}


class TestParentRequests:
  @pytest.mark.parametrize("full_first", [True, False])
  def test_a_child_followup_cannot_displace_a_full_repair_request(self, manager, full_first):
    for repair in [full_first, not full_first]:
      manager.request_discovery_sync(repair=repair)

    (task,) = manager.task_snapshot()["queued"]
    assert task["name"] == "sync_discovery" and task["params"] == {"repair": True}

  @pytest.mark.parametrize("failure", ["DNS", "offline"])
  def test_a_failed_or_offline_child_does_not_form_an_immediate_retry_loop(self, manager, application, failure):
    container = Container.create(application=application, worker="w1", status="active")
    domain = Domain.create(name="shop", type="internal", port=80, container=container, service="shop")
    if failure == "DNS":
      manager.cloudflare.failure = OSError("unavailable")
    else:
      manager.online.remove("w1")
    manager.request_discovery_sync()

    async def drain():
      for _ in range(1000):
        if not manager.task_snapshot()["queued"] and not manager.task_snapshot()["running"]:
          return
        manager.dispatch_tick()
        await asyncio.sleep(0.001)
      pytest.fail("Domain work did not drain")
    asyncio.run(drain())

    assert Domain.get_by_id(domain.id).status == ("error" if failure == "DNS" else "pending")
    assert not manager.task_snapshot()["queued"]


@pytest.mark.parametrize("actual, desired, matches", [
    ({"content": "target", "proxied": True}, {"content": "target", "proxied": True}, True),
    ({"content": "target", "proxied": False}, {"content": "target", "proxied": True}, False),
    ({"content": "target"}, {"content": "target", "proxied": True}, False),
    ({"content": "target", "other": 1}, {"content": "target"}, True),
])
def test_dns_matching_checks_requested_fields_only(actual, desired, matches):
  assert dns_record_matches(actual, desired) is matches
