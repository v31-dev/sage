import asyncio
import contextlib
import logging
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.backups.keys import taken_at
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.containers.record import TargetRead
from services.containers.targets import Containers
from services.db.models import Backup, ContainerEvent, Instance, Notification, Worker, db
from services.domains.decisions import ApplyOutcome
from services.domains.state import Domains
from services.logs import Logs
from services.manager.backup_catalog import BackupCatalogMixin
from services.manager.platform import PlatformMixin
from services.manager.queue import SCOPES, QueueMixin
from services.manager.settings import SettingsMixin
from services.metrics import Metrics
from services.notification import Notifications
from services.releases import Releases
from services.s3 import S3
from services.settings import Settings
from services.tailscale import Tailscale
from services.workers.state import Workers
from utils.archive import decrypt_file
from utils.errors import Conflict
from utils.executor import active_executor
from utils.logging import TaskFailed
from utils.queue import Outcome, TaskQueue

STORE_A = {"access_key": "a", "secret_key": "a", "bucket": "a", "endpoint": "https://a.example.test", "path": "base"}
STORE_B = {**STORE_A, "bucket": "b"}


class Store:
  """A store as a folder: an upload copies the file to its key, a download copies it back; `fails` fails both."""

  def __init__(self, root: Path, identity: str):
    self.root, self.identity, self.fails = root, identity, None

  def get_full_path(self, key):
    return f"base/{key.strip('/')}"

  async def upload_to_key(self, file_path, s3_path):
    if self.fails:
      raise Exception(self.fails)
    key = f"{self.get_full_path(s3_path)}/{Path(file_path).name}"
    (self.root / key).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(file_path, self.root / key)
    return key

  async def download_key(self, s3_path, local_path, raw_path=False):
    if self.fails:
      raise Exception(self.fails)
    shutil.copy(self.root / s3_path, local_path)

  async def delete_key(self, s3_path, filter=None, raw_path=False):
    (self.root / s3_path).unlink()
    return [s3_path]

  def keys(self) -> set[str]:
    return {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}


class Manager(PlatformMixin, QueueMixin):
  """The platform's part of the Manager; what it requests of the other parts is recorded."""

  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=1)
    self.queue_facts = []
    self.requested = []
    self.setup_failures, self.stack_repairs, self.recoveries, self.orphans = {"w1": 1.0}, {"w1": 1.0}, {"c": 1}, {"o": 1}
    self.task_queue = TaskQueue(SCOPES, {lane: self.pool for lane in ("platform", "app", "common", "metrics")},
                                record=lambda task, status: None, notify=self._queue_changed)

  def __getattr__(self, name):
    if name.startswith("request_"):
      return lambda: self.requested.append(name)
    raise AttributeError(name)

  async def run(self):
    for _ in range(500):
      if not (self.task_queue.snapshot()["running"] or self.task_queue.snapshot()["queued"]):
        return
      self.task_queue.dispatch_tick()
      await asyncio.sleep(0.01)


@pytest.fixture
def stores(tmp_path, register):
  stores = SimpleNamespace(a=Store(tmp_path / "a", "sa"), b=Store(tmp_path / "b", "sb"))
  stores.configured = stores.a

  def session():
    if stores.configured is None:
      raise Conflict("S3 is not configured.")
    return stores.configured

  register(S3, SimpleNamespace(session=session, install=lambda values: stores.installed.append(values)))
  stores.installed = []
  return stores


@pytest.fixture
def manager(db, stores, register):
  notices = []
  register(Notifications, SimpleNamespace(install=lambda values: None, notify=lambda *notice: notices.append(notice)))
  manager = Manager()
  manager.notices = notices
  manager.fetched = []
  register(Releases, SimpleNamespace(refresh=lambda: manager.fetched.append(True)))
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


pytestmark = pytest.mark.usefixtures("thread_connections")


def tables(path):
  with contextlib.closing(sqlite3.connect(path)) as copy:
    return {table: copy.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for (table,) in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


class TestBackup:
  def test_it_holds_its_part_of_the_store_and_is_not_queued_without_one(self, manager, stores):
    manager.request_platform_backup()
    [task] = manager.task_queue.snapshot()["queued"]
    assert (tuple(task["key"]), task["scopes"], task["params"], task["quiet"]) == (
        ("backup_platform",), ["backup:store:sa:write:platform"], {"storage_id": "sa"}, False)
    stores.configured = None
    assert manager.request_platform_backup() is None

  async def test_the_configuration_is_uploaded_encrypted_and_recorded_by_the_key_uploaded(
          self, manager, stores, worker, tmp_path):
    Notification.create(content="Deployed.")
    await manager.backup_platform("sa")
    [row] = Backup.select()
    assert (row.storage_id, row.type, row.s3_path.startswith("base/backups/platform/")) == ("sa", "platform", True)
    decrypt_file(stores.a.root / row.s3_path, tmp_path / "plain.db")
    held = tables(tmp_path / "plain.db")
    assert (held["worker"], held["notification"], held["backup"]) == (1, 0, 0)
    assert manager.notices == [("Platform backup created.", "success", "/backups")]

  async def test_it_is_refused_once_the_store_changed_and_a_failed_upload_fails_it(self, manager, stores):
    stores.configured = stores.b
    with pytest.raises(TaskFailed):
      await manager.backup_platform("sa")
    stores.configured, stores.a.fails = stores.a, "AccessDenied"
    with pytest.raises(TaskFailed):
      await manager.backup_platform("sa")
    assert Backup.select().count() == 0
    assert manager.notices == [("The platform backup failed: AccessDenied", "error", "/backups")]

  async def test_two_taken_in_the_same_second_are_two_backups(self, manager, stores, clock):
    await manager.backup_platform("sa")
    await manager.backup_platform("sa")
    paths = [row.s3_path for row in Backup.select()]
    assert len(set(paths)) == 2 and stores.a.keys() == set(paths)

  async def test_deleting_one_while_the_next_uploads_in_the_same_second_leaves_the_next(self, stores, register, clock):
    class Catalog(Manager, BackupCatalogMixin):
      pass

    register(Notifications, SimpleNamespace(notify=lambda *notice: None))
    manager, uploading, release = Catalog(), asyncio.Event(), asyncio.Event()
    upload = stores.a.upload_to_key

    async def held(file_path, s3_path):
      key = await upload(file_path, s3_path)
      if Backup.select().count():
        uploading.set()
        await release.wait()
      return key

    stores.a.upload_to_key = held
    token = active_executor.set(manager.pool)
    try:
      manager.request_platform_backup()
      await manager.run()
      [first] = Backup.select()
      manager.request_platform_backup()
      manager.task_queue.dispatch_tick()
      await asyncio.wait_for(uploading.wait(), 5)
      manager.request_backup_deletion(first.id)
      for _ in range(500):
        manager.task_queue.dispatch_tick()
        if [task["name"] for task in manager.task_queue.snapshot()["running"]] == ["backup_platform"]:
          break
        await asyncio.sleep(0.01)
      assert first.s3_path not in stores.a.keys()
      release.set()
      await manager.run()
    finally:
      active_executor.reset(token)
      manager.pool.shutdown(wait=False, cancel_futures=True)
    [second] = Backup.select()
    assert stores.a.keys() == {second.s3_path}


class TestRestoreRequest:
  def test_only_a_platform_backup_of_the_configured_store_is_restored(self, manager, stores):
    with pytest.raises(LookupError):
      manager.request_platform_restore(999)
    archive = Backup.create(s3_path="base/backups/applications/x", storage_id="sa", type="application")
    with pytest.raises(ValueError):
      manager.request_platform_restore(archive.id)
    elsewhere = Backup.create(s3_path="base/backups/platform/x.db.enc", storage_id="sb", type="platform")
    with pytest.raises(Conflict):
      manager.request_platform_restore(elsewhere.id)

  def test_it_waits_as_the_restore_and_names_its_backup_while_restoring(self, manager, stores):
    backup = Backup.create(s3_path="base/backups/platform/20260101_030000_4444444444444444.db.enc", storage_id="sa", type="platform")
    manager.request_platform_restore(backup.id)
    [task] = manager.task_queue.snapshot()["queued"]
    assert (task["name"], task["key"], task["scopes"], task["executor"], task["params"]) == (
        "restore_platform", ["restore_platform", backup.s3_path], [], "platform", {"s3_path": backup.s3_path, "storage_id": "sa"})
    assert (manager.task_state(), manager.restoring_from()) == ("restoring", "2026-01-01T03:00:00")

  def test_the_backup_being_restored_is_the_one_its_key_names_whatever_its_params_say(self, manager, stores):
    manager.add_restore(task=manager.restore_platform, name="restore_platform", executor="platform",
                        key=("restore_platform", "base/backups/platform/20260101_030000_4444444444444444.db.enc"),
                        params={"s3_path": "base/backups/platform/20250101_030000_4444444444444444.db.enc", "storage_id": "sa"})
    assert manager.restoring_from() == "2026-01-01T03:00:00"

  def test_every_other_request_is_refused_while_it_waits(self, manager, stores):
    backup = Backup.create(s3_path="base/backups/platform/20260101_030000_4444444444444444.db.enc", storage_id="sa", type="platform")
    manager.request_platform_restore(backup.id)
    assert manager.add_task(task=asyncio.sleep, key=("later",), scopes=set(), executor="app").outcome == Outcome.REFUSED
    with pytest.raises(Conflict, match="not running"):
      manager.request_platform_restore(backup.id)
    assert [task["name"] for task in manager.task_queue.snapshot()["queued"]] == ["restore_platform"]


class TestRestore:
  @pytest.fixture
  async def backup(self, manager, stores, worker):
    """A platform backup taken with store A's settings saved, then the live configuration changed: another worker, store
    B's settings, a notification, memory of the old configuration."""
    Settings().set("s3", STORE_A)
    await manager.backup_platform("sa")
    row = Backup.get()
    Worker.create(hostname="w2", subdomain="blue")
    Settings().set("s3", STORE_B)
    Notification.create(content="Added w2.")
    Containers()._reads["c"] = TargetRead("absent")
    Workers().record_setup("w1", "succeeded")
    Domains().record_outcome(SimpleNamespace(id="d1", container=None, namespace_worker_id=None), ApplyOutcome("f", True))
    return row

  async def test_it_restores_the_configuration_as_a_restart_would_find_it(self, manager, stores, backup, signals):
    generation, ran = Instance.get().state_generation, []

    async def waiting():
      ran.append(Worker.select().count())

    manager.add_task(task=waiting, key=("old",), scopes=set(), executor="app")
    manager.request_platform_restore(backup.id)
    await manager.run()
    assert [worker.hostname for worker in Worker.select()] == ["w1"]
    assert Notification.select().count() == 0 and Instance.get().state_generation != generation
    assert Settings().get("s3") == STORE_B and stores.installed[-1] == STORE_B
    assert (Containers().target("c"), Workers().setup("w1"), Domains().outcome("d1")) == (TargetRead("unobserved"), None, None)
    assert (manager.setup_failures, manager.stack_repairs, manager.recoveries, manager.orphans) == ({}, {}, {}, {})
    assert manager.task_state() == "running" and manager.task_queue.snapshot()["queued"] == []
    assert manager.requested == STARTUP and manager.fetched == [True]
    assert ran == [2], "the task queued before the restore ran first, on the configuration it was queued against"
    assert manager.notices[-1] == (f"Platform restored from the backup taken at {taken_at(backup.s3_path)}.", "success", "/backups")

  async def test_a_failed_download_changes_nothing(self, manager, stores, backup):
    manager.request_platform_restore(backup.id)
    stores.a.fails = "AccessDenied"
    await manager.run()
    assert sorted(worker.hostname for worker in Worker.select()) == ["w1", "w2"] and Settings().get("s3") == STORE_B
    assert Workers().setup("w1") == "succeeded" and manager.requested == []
    assert manager.task_state() == "running"
    assert manager.notices[-1] == (f"The platform restore from the backup taken at {taken_at(backup.s3_path)} failed; nothing changed: "
                                   "AccessDenied", "error", "/backups")

  async def test_a_restore_refused_once_the_store_changed_changes_nothing(self, manager, stores, backup):
    (stores.b.root / backup.s3_path).parent.mkdir(parents=True)
    shutil.copy(stores.a.root / backup.s3_path, stores.b.root / backup.s3_path)
    stores.configured = stores.b
    with pytest.raises(TaskFailed):
      await manager.restore_platform(backup.s3_path, "sa")
    assert Worker.select().count() == 2 and manager.requested == []

  async def test_the_history_starts_empty(self, manager, stores, worker, application):
    from services.db.models import Container
    Settings().set("s3", STORE_A)
    await manager.backup_platform("sa")
    row = Backup.get()
    container = Container.create(application=application, worker=worker)
    ContainerEvent.create(container=container, subject_id=container.id, action="deploy", transition="admitted",
                          application_name="demo-web", worker_hostname="w1")
    manager.request_platform_restore(row.id)
    await manager.run()
    assert (ContainerEvent.select().count(), Notification.select().count(), Backup.select().count()) == (0, 0, 0)


class TestFacts:
  async def test_the_backup_status_follows_a_running_platform_backup_and_a_discovery_queued(self, manager):
    assert manager.backup_status() == {"platform_backup_in_progress": False, "discovery_in_progress": False}
    manager.add_task(task=asyncio.sleep, key=("backup_platform",), scopes=set(), executor="platform", params={"delay": 0})
    manager.add_task(task=asyncio.sleep, key=("discover_backups",), scopes={"backup:store:sa"}, executor="platform",
                     params={"delay": 0})
    assert manager.backup_status() == {"platform_backup_in_progress": False, "discovery_in_progress": True}
    manager.task_queue.dispatch_tick()
    assert manager.backup_status()["platform_backup_in_progress"] is True
    await manager.run()

  def test_nothing_is_being_restored_outside_a_restore_or_from_a_key_without_a_time(self, manager, stores):
    assert manager.restoring_from() is None
    backup = Backup.create(s3_path="base/backups/platform/latest.db.enc", storage_id="sa", type="platform")
    manager.request_platform_restore(backup.id)
    assert (manager.task_state(), manager.restoring_from()) == ("restoring", None)

  def test_a_restore_is_refused_while_sage_is_stopping(self, manager, stores):
    backup = Backup.create(s3_path="base/backups/platform/20260101_030000_4444444444444444.db.enc", storage_id="sa", type="platform")
    manager.stop_tasks()
    with pytest.raises(Conflict, match="not running"):
      manager.request_platform_restore(backup.id)

  def test_its_facts_are_the_restore_and_the_backup_status(self, manager):
    facts = manager.platform_queue_facts()
    assert [fact._compute() for fact in facts] == [None, manager.backup_status()]


STARTUP = ["request_worker_observation", "request_worker_paths", "request_container_observation",
           "request_certificate_renewal", "request_domain_reconciliation", "request_backup_discovery",
           "request_settings_checks"]


def queued(manager):
  return [(tuple(task["key"]), task["scopes"], task["executor"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]]


class TestStartup:
  def test_startup_requests_what_runs_when_sage_starts_and_checks_for_a_release_quietly(self, manager):
    manager.request_startup()
    assert manager.requested == STARTUP
    assert queued(manager) == [(("refresh_latest_release",), ["release"], "common", True)]

  def test_a_release_check_asked_for_is_visible_and_one_still_queued_is_replaced(self, manager):
    manager.request_release_check()
    manager.request_release_check(quiet=False)
    assert queued(manager) == [(("refresh_latest_release",), ["release"], "common", False)]


class TestMetricsFlush:
  @pytest.fixture
  def flushed(self, register):
    flushed = []
    register(Metrics, SimpleNamespace(flush=lambda: flushed.append(True)))
    return flushed

  @staticmethod
  async def holding():
    await asyncio.sleep(0.05)

  def test_it_is_one_quiet_task_on_the_metrics_lane(self, manager, flushed):
    manager.request_metrics_flush()
    manager.request_metrics_flush()
    assert queued(manager) == [(("flush_metrics",), ["metrics:flush"], "metrics", True)]

  async def test_it_waits_while_the_daily_metrics_cleanup_runs(self, manager, flushed):
    manager.add_task(task=self.holding, key=("metrics_cleanup",), scopes={"metrics"}, executor="metrics")
    manager.task_queue.dispatch_tick()
    manager.request_metrics_flush()
    manager.task_queue.dispatch_tick()
    assert [name for (name,), *_ in queued(manager)] == ["flush_metrics"]
    await manager.run()
    assert flushed == [True]

  async def test_it_runs_while_other_work_holds_its_scopes(self, manager, flushed):
    manager.add_task(task=self.holding, key=("held",), scopes={"worker", "domain", "backup", "settings", "release"}, executor="app")
    manager.task_queue.dispatch_tick()
    manager.request_metrics_flush()
    manager.task_queue.dispatch_tick()
    assert queued(manager) == []
    await manager.run()
    assert flushed == [True]


class TestCleanup:
  @pytest.fixture
  def cleaned(self, register):
    cleaned = []
    register(Metrics, SimpleNamespace(cleanup=lambda: cleaned.append("metrics")))
    register(Logs, SimpleNamespace(cleanup=lambda: cleaned.append("logs")))
    return cleaned

  def test_each_part_is_a_quiet_task_of_its_own_and_the_store_is_asked_to_prune(self, manager, cleaned):
    manager.request_cleanup()
    manager.request_cleanup()
    assert queued(manager) == [(("metrics_cleanup",), ["metrics"], "metrics", True), (("logs_cleanup",), [], "common", True),
                               (("purge_history",), [], "common", True)]
    assert manager.requested == ["request_backup_retention"] * 2

  async def test_the_tasks_run_while_other_work_holds_its_scopes(self, manager, cleaned, clock):
    Notification.create(content="Deployed.")
    clock.advance(timedelta(days=15))

    async def holding():
      await asyncio.sleep(0.05)

    manager.add_task(task=holding, key=("held",), scopes={"worker", "domain", "backup", "settings", "release"}, executor="app")
    manager.task_queue.dispatch_tick()
    manager.request_cleanup()
    manager.task_queue.dispatch_tick()
    assert {task["name"] for task in manager.task_queue.snapshot()["running"]} >= {"held", "metrics_cleanup"}
    await manager.run()
    assert cleaned == ["metrics", "logs"] and Notification.select().count() == 0


class TestInfo:
  class Shown(PlatformMixin, SettingsMixin, QueueMixin):
    """The platform's and the settings' parts of the Manager, with the real settings status."""

    def __init__(self):
      self.version, self.started_at = "1.0.0", datetime(2026, 1, 1, 9)
      self.task_queue = TaskQueue(SCOPES, {}, record=lambda task, status: None)

  @pytest.fixture
  def shown(self, db, register):
    register(Tailscale, SimpleNamespace(ip=lambda: "100.64.0.1"))
    register(Releases, SimpleNamespace(latest_version="9.9.9"))
    register(Certs, SimpleNamespace(status=lambda: []))
    register(Cloudflare, SimpleNamespace(account_id="a1", tunnel_id="t1", tunnel_name="sage-example.test", tunnel_status="healthy"))
    Settings()
    return self.Shown()

  def test_it_is_what_every_page_shows_of_the_manager(self, shown):
    info = shown.info()
    assert {key: value for key, value in info.items() if key != "settings"} == {
        "version": "1.0.0", "latest_version": "9.9.9", "domain": "example.test", "name": "manager", "ip": "100.64.0.1",
        "start_time": "2026-01-01T09:00:00", "state": "running", "restoring_from": None}
    assert set(info["settings"]) == {"tailscale", "cloudflare", "s3", "notifications"}

  def test_it_reads_no_database(self, shown, monkeypatch):
    def refuse(*args, **kwargs):
      raise AssertionError("The info read the database.")

    monkeypatch.setattr(db, "execute_sql", refuse)
    assert shown.info()["settings"]["s3"]["state"] == "not_configured"

  def test_a_settings_status_that_cannot_be_built_is_none_and_logged(self, shown, monkeypatch, caplog):
    def fails():
      raise RuntimeError("no status")

    monkeypatch.setattr(shown, "settings_status", fails)
    with caplog.at_level(logging.ERROR):
      assert shown.info()["settings"] is None
    assert "Could not build the settings status." in caplog.text
