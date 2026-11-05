import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from routes.domains import domain_payload
from routes.notifications import list_notifications
from services.cloudflare import CloudflareError
from services.db import Domain, Task, Worker
from services.manager import Manager
from services.manager.core import CoreMixin
from services.manager.queue import SCOPES
from services.notification import Notifications
from services.updates import UpdateFeed, Updates
from tests.services.manager.test_discovery_health import drain
from tests.services.manager.test_domain_reconciliation import Reconciling, applied
from utils.executor import active_executor
from utils.queue import TaskQueue


class Reporting(CoreMixin, Reconciling):
  pass


@pytest.fixture
def manager(tmp_path, db, thread_connections, register):
  feeds = []
  register(Updates, SimpleNamespace(changed=lambda *args: None,
                                    publish=lambda feed, rows: feeds.append((feed, rows))))
  register(Notifications, SimpleNamespace(dispatch=lambda notification: None))
  Worker.create(hostname="w1", ip="100.64.0.2", subdomain="red")
  Worker.create(hostname="w2", ip="100.64.0.3", subdomain="blue")
  with ThreadPoolExecutor(max_workers=2) as pool:
    token = active_executor.set(pool)
    manager = Reporting(tmp_path, pool)
    manager.feeds = feeds
    manager.task_queue = TaskQueue(SCOPES, {"app": pool}, record=manager._persist_task, notify=manager._queue_changed)
    register(Manager, manager)
    yield manager
    manager.task_queue.stop()
    active_executor.reset(token)


@pytest.mark.parametrize("status, reason", [(400, "DNS record quota exceeded"), (429, "API rate limit exceeded")])
def test_a_dns_failure_reaches_the_record_notifications_and_push_feed(manager, application, status, reason):
  domain = applied(manager, application, type="public")
  manager.cloudflare.failure = CloudflareError(status, reason)

  with pytest.raises(CloudflareError):
    manager.run(domain.id, application.id)

  stored = Domain.get_by_id(domain.id)
  assert domain_payload(stored)["status"] == "error"
  assert domain_payload(stored)["sync_error"].startswith(f"DNS: Cloudflare answered HTTP {status}")
  assert (stored.synced_hash, stored.synced_at) == (domain.synced_hash, domain.synced_at)
  (notice,) = list_notifications()
  assert notice["type"] == "error" and notice["link"] == "/domains"
  assert "shop.example.test sync failed at DNS" in notice["content"]
  assert reason in notice["content"]
  assert [(feed, rows[0]["id"]) for feed, rows in manager.feeds] == [(UpdateFeed.NOTIFICATIONS, notice["id"])]


def test_a_routing_failure_names_the_record_and_phase(manager, application, monkeypatch):
  domain = applied(manager, application)

  async def write(*args, **kwargs):
    raise OSError("worker write refused")
  monkeypatch.setattr(manager.tailscale, "sync_file", write)

  with pytest.raises(OSError):
    manager.run(domain.id, application.id)

  (notice,) = list_notifications()
  assert "shop.red.int.example.test sync failed at Traefik: worker write refused" in notice["content"]
  assert Domain.get_by_id(domain.id).sync_error == "Traefik: worker write refused"


@pytest.mark.parametrize("online", [True, False])
def test_a_success_or_offline_pending_record_does_not_notify_an_error(manager, application, online):
  domain = applied(manager, application)
  if not online:
    manager.online.remove("w1")

  manager.run(domain.id, application.id)

  assert Domain.get_by_id(domain.id).status == ("synced" if online else "pending")
  assert list_notifications() == []


@pytest.mark.parametrize("source", ["DNS inventory", "Tunnel inventory", "Discovery DNS", "worker write"])
def test_a_failed_parent_notifies_once_and_preserves_successful_domain_proof(manager, application, monkeypatch, source):
  domain = applied(manager, application)
  error = CloudflareError(429, "API rate limit exceeded", "300")
  if source == "DNS inventory":
    manager.cloudflare.zone_error = error
  elif source == "Tunnel inventory":
    def inventory():
      raise error
    monkeypatch.setattr(manager.cloudflare, "tunnels", inventory)
  elif source == "Discovery DNS":
    manager.cloudflare.failure = error
  else:
    write = manager.tailscale.sync_file

    async def failing_write(hostname, *args, **kwargs):
      if hostname == "w1":
        raise OSError("worker write refused")
      return await write(hostname, *args, **kwargs)
    monkeypatch.setattr(manager.tailscale, "sync_file", failing_write)

  with pytest.raises(RuntimeError, match="Domain reconciliation failed"):
    manager.reconcile(repair=False)

  stored = Domain.get_by_id(domain.id)
  assert (stored.status, stored.sync_error, stored.synced_hash, stored.synced_at) == (
      "synced", None, domain.synced_hash, domain.synced_at)
  (notice,) = list_notifications()
  assert (notice["type"], notice["link"]) == ("error", "/domains")
  assert "Domain reconciliation failed" in notice["content"]
  assert ("worker write refused" if source == "worker write" else "API rate limit exceeded") in notice["content"]
  assert "_discovery-internal-shop.yml" in manager.tailscale.files("w2")


def test_a_successful_parent_does_not_create_an_error_notification(manager, application):
  applied(manager, application)

  manager.reconcile(repair=False)

  assert list_notifications() == []


def test_failed_record_and_parent_tasks_surface_errors_and_then_settle(manager, application):
  domain = applied(manager, application)
  asyncio.run(drain(manager))
  manager.cloudflare.failure = CloudflareError(429, "API rate limit exceeded")
  manager.cloudflare.zone_error = manager.cloudflare.failure
  manager.request_domain_sync(domain)

  asyncio.run(drain(manager))

  stored = Domain.get_by_id(domain.id)
  assert domain_payload(stored)["status"] == "error"
  assert "API rate limit exceeded" in domain_payload(stored)["sync_error"]
  assert (stored.synced_hash, stored.synced_at) == (domain.synced_hash, domain.synced_at)
  assert {(task.name, task.status) for task in Task.select()} == {("sync_domain", "failed"), ("sync_discovery", "failed")}
  notices = list_notifications()
  assert len(notices) == 2
  assert all(notice["type"] == "error" and "API rate limit exceeded" in notice["content"] for notice in notices)
  assert manager.task_snapshot()["queued"] == []
