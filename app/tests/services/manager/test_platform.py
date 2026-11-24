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

import config
from services.backups.commands import add_backups, settle_backups
from services.backups.keys import archive_name, taken_at
from services.certs import Certs
from services.cloudflare import Cloudflare
from services.containers.record import TargetRead
from services.containers.targets import Containers
from services.db import migrations
from services.db.database import Database
from services.db.migration_log import record
from services.db.models import Backup, ContainerEvent, Instance, Notification, Worker, db
from services.domains.decisions import ApplyOutcome
from services.domains.state import Domains
from services.logs import Logs
from services.manager.backup_catalog import BackupCatalogMixin
from services.manager.backups import settling
from services.manager.platform import PlatformMixin
from services.manager.queue import SCOPES, TASK_NAMES, QueueMixin
from services.manager.settings import SettingsMixin
from services.metrics import Metrics
from services.notification import Notifications
from services.releases import Releases
from services.s3 import S3
from services.settings import Settings
from services.tailscale import Tailscale
from services.workers.state import Workers
from utils.archive import decrypt_file
from utils.common import utcnow
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
    self.pool, self.started_at = ThreadPoolExecutor(max_workers=1), datetime(2000, 1, 1)
    self.queue_facts = []
    self.requested = []
    self.setup_failures, self.stack_repairs, self.removal_failures = {"w1": 1.0}, {"w1": 1.0}, {"w1": 1.0}
    self.recoveries, self.orphans = {"c": 1}, {"o": 1}
    self.task_queue = TaskQueue(SCOPES, {lane: self.pool for lane in ("main", "common")},
                                record=lambda task, status: None, notify=self._queue_changed,
                                names=TASK_NAMES | {"held", "later", "old"})

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

  register(S3, SimpleNamespace(session=session, install=lambda values, revision: stores.installed.append(values)))
  stores.installed = []
  return stores


@pytest.fixture
def manager(db, stores, register):
  notices = []
  register(Notifications, SimpleNamespace(install=lambda values, revision: None, notify=lambda *notice: notices.append(notice)))
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


async def take(manager, storage_id="sa"):
  """A platform backup recorded queued and then taken, as its request and the queue do; its row."""
  [backup_id] = add_backups(storage_id, utcnow(), False, [f"base/backups/platform/{archive_name(utcnow())}_0.2.0_1.db.enc"])
  await settling(manager.backup_platform)(backup_ids=[backup_id], storage_id=storage_id)
  return Backup.get_by_id(backup_id)


class TestBackup:
  def test_it_holds_its_part_of_the_store_with_its_backup_queued_and_is_not_queued_without_one(self, manager, stores, clock):
    manager.request_platform_backup()
    [task] = manager.task_queue.snapshot()["queued"]
    [row] = Backup.select()
    assert (tuple(task["key"]), task["scopes"], task["params"], task["quiet"]) == (
        ("backup_platform",), ["backup:store:sa:write:platform"], {"backup_ids": [row.id], "storage_id": "sa"}, False)
    assert (row.type, row.storage_id, row.status, row.slot_at, row.automatic, row.s3_path.startswith("base/backups/platform/")) == (
        "platform", "sa", "queued", clock.current, False, True)
    stores.configured = None
    assert manager.request_platform_backup() is None and Backup.select().count() == 1

  def test_its_key_names_this_release_and_the_databases_version(self, manager, stores, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "1.2.3")
    manager.request_platform_backup()
    assert Backup.get().s3_path.endswith(f"_1.2.3_{len(migrations.STEPS)}.db.enc")

  def test_the_schedules_backup_is_automatic(self, manager, stores):
    manager.request_platform_backup(utcnow())
    assert Backup.get().automatic

  def test_the_schedules_is_skipped_while_its_slot_is_held_and_takes_its_left_backup_again(self, manager, stores, clock):
    slot = clock.current.replace(second=0, microsecond=0)
    settle_backups(add_backups("sa", slot - timedelta(minutes=3), False, ["base/backups/platform/m.db.enc"]), "succeeded")
    assert manager.request_platform_backup(clock.current) is None
    Backup.delete().execute()
    [left] = add_backups("sa", slot, True, ["base/backups/platform/l.db.enc"])
    settle_backups([left], "interrupted")
    manager.request_platform_backup(clock.current)
    [task] = manager.task_queue.snapshot()["queued"]
    assert task["params"]["backup_ids"] == [left] and Backup.get_by_id(left).status == "queued"

  def test_the_catch_up_asks_for_each_schedules_most_recent_slot_before_the_start(self, manager, stores):
    manager.started_at, asked = datetime(2026, 1, 2, 9, 30), []
    manager.request_automatic_backups = lambda now, since_start=False: asked.append(since_start)
    manager.request_backup_catch_up("0 */12 * * *")
    [row] = Backup.select()
    assert asked == [True] and (row.slot_at, row.automatic, row.status) == (datetime(2026, 1, 2, 0, 0), True, "queued")

  def test_one_replacing_a_queued_one_settles_that_one_cancelled(self, manager, stores):
    first = manager.request_platform_backup()
    second = manager.request_platform_backup()
    assert [task["task_id"] for task in manager.task_queue.snapshot()["queued"]] == [second] != [first]
    assert sorted(row.status for row in Backup.select()) == ["cancelled", "queued"]

  def test_one_the_queue_does_not_take_leaves_no_backup(self, manager, stores):
    manager.task_queue.stop()
    manager.request_platform_backup()
    assert Backup.select().count() == 0

  async def test_the_configuration_is_uploaded_encrypted_and_recorded_by_the_key_uploaded(
          self, manager, stores, worker, tmp_path):
    Notification.create(content="Deployed.")
    await take(manager)
    [row] = Backup.select()
    assert (row.storage_id, row.type, row.status, row.s3_path.startswith("base/backups/platform/")) == (
        "sa", "platform", "succeeded", True)
    assert stores.a.keys() == {row.s3_path}
    decrypt_file(stores.a.root / row.s3_path, tmp_path / "plain.db")
    held = tables(tmp_path / "plain.db")
    assert (held["worker"], held["notification"], held["backup"]) == (1, 0, 0)
    assert manager.notices == [("Platform backup created.", "success", "/backups")]

  async def test_it_is_refused_once_the_store_changed_and_a_failed_upload_fails_it(self, manager, stores):
    stores.configured = stores.b
    with pytest.raises(TaskFailed):
      await take(manager)
    stores.configured, stores.a.fails = stores.a, "AccessDenied"
    with pytest.raises(TaskFailed):
      await take(manager)
    assert [(row.status, row.reason) for row in Backup.select().order_by(Backup.created_at, Backup.reason.desc())] == [
        ("failed", "the backup store is not the one it was requested for"), ("failed", "AccessDenied")]
    assert manager.notices == [("The platform backup failed: AccessDenied", "error", "/backups")]

  async def test_its_copies_are_made_in_the_managers_temp_folder_and_gone_after(self, manager, stores, monkeypatch):
    snapshot, made = Database().snapshot, []
    monkeypatch.setattr(Database(), "snapshot", lambda path: made.append(path) or snapshot(path))
    await take(manager)
    assert [path.parent.parent for path in made] == [config.TMP_DIR] and list(config.TMP_DIR.iterdir()) == []

  async def test_two_taken_in_the_same_second_are_two_backups(self, manager, stores, clock):
    await take(manager)
    await take(manager)
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
      if Backup.select().where(Backup.status == "succeeded").count():
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
    [second] = Backup.select().where(Backup.deleted_at.is_null())
    assert stores.a.keys() == {second.s3_path}


class TestRestoreRequest:
  def test_an_old_reference_cannot_restore_a_replacement(self, manager, stores):
    first = Backup.create(s3_path="old", storage_id="sa", type="platform")
    first.delete_instance()
    second = Backup.create(s3_path="new", storage_id="sa", type="platform")
    assert second.id != first.id

    with pytest.raises(LookupError):
      manager.request_platform_restore(first.id)
    assert manager.task_state() == "running" and manager.task_snapshot() == {"running": [], "queued": []}

  def test_only_a_platform_backup_of_the_configured_store_is_restored(self, manager, stores):
    with pytest.raises(LookupError):
      manager.request_platform_restore("missing")
    archive = Backup.create(s3_path="base/backups/applications/x", storage_id="sa", type="application")
    with pytest.raises(ValueError):
      manager.request_platform_restore(archive.id)
    elsewhere = Backup.create(s3_path="base/backups/platform/x.db.enc", storage_id="sb", type="platform")
    with pytest.raises(Conflict):
      manager.request_platform_restore(elsewhere.id)
    failed = Backup.create(s3_path="base/backups/platform/y.db.enc", storage_id="sa", type="platform", status="failed")
    with pytest.raises(Conflict, match="Only a completed backup"):
      manager.request_platform_restore(failed.id)
    deleted = Backup.create(s3_path="base/backups/platform/z.db.enc", storage_id="sa", type="platform", deleted_at=utcnow())
    with pytest.raises(LookupError):
      manager.request_platform_restore(deleted.id)

  def test_a_backup_a_later_sage_took_is_refused_before_anything_is_queued(self, manager, stores):
    later = Backup.create(s3_path=f"base/backups/platform/20260101_030000_4444444444444444_9.0.0_{len(migrations.STEPS) + 1}.db.enc",
                          storage_id="sa", type="platform")
    with pytest.raises(Conflict, match="A backup from a later version of Sage can't be restored."):
      manager.request_platform_restore(later.id)
    assert manager.task_state() == "running" and manager.task_snapshot() == {"running": [], "queued": []}

  def test_it_waits_as_the_restore_and_names_its_backup_while_restoring(self, manager, stores):
    backup = Backup.create(s3_path="base/backups/platform/20260101_030000_4444444444444444_0.2.0_1.db.enc", storage_id="sa", type="platform")
    manager.request_platform_restore(backup.id)
    [task] = manager.task_queue.snapshot()["queued"]
    assert (task["name"], task["key"], task["scopes"], task["executor"], task["params"]) == (
        "restore_platform", ["restore_platform", backup.s3_path], [], "main", {"s3_path": backup.s3_path, "storage_id": "sa"})
    assert (manager.task_state(), manager.restoring_from()) == ("restoring", "2026-01-01T03:00:00")

  def test_the_backup_being_restored_is_the_one_its_key_names_whatever_its_params_say(self, manager, stores):
    manager.add_restore(task=manager.restore_platform, name="restore_platform", executor="main",
                        key=("restore_platform", "base/backups/platform/20260101_030000_4444444444444444_0.2.0_1.db.enc"),
                        params={"s3_path": "base/backups/platform/20250101_030000_4444444444444444_0.2.0_1.db.enc", "storage_id": "sa"})
    assert manager.restoring_from() == "2026-01-01T03:00:00"

  def test_every_other_request_is_refused_while_it_waits(self, manager, stores):
    backup = Backup.create(s3_path="base/backups/platform/20260101_030000_4444444444444444_0.2.0_1.db.enc", storage_id="sa", type="platform")
    manager.request_platform_restore(backup.id)
    assert manager.add_task(task=asyncio.sleep, key=("later",), scopes=set(), executor="main").outcome == Outcome.REFUSED
    with pytest.raises(Conflict, match="not running"):
      manager.request_platform_restore(backup.id)
    assert [task["name"] for task in manager.task_queue.snapshot()["queued"]] == ["restore_platform"]


class TestRestore:
  @pytest.fixture
  async def backup(self, manager, stores, worker):
    """A platform backup taken with store A's settings saved, then the live configuration changed: another worker, store
    B's settings, a notification, memory of the old configuration."""
    Settings().set("s3", STORE_A)
    await take(manager)
    row = Backup.get()
    Worker.create(hostname="w2", subdomain="blue")
    Settings().set("s3", STORE_B)
    Notification.create(content="Added w2.")
    Containers()._reads["c"] = TargetRead("absent")
    Workers().record_setup("w1", "succeeded")
    container = SimpleNamespace(worker_id="w1", application=SimpleNamespace(name="web", project=SimpleNamespace(name="demo")))
    Domains().record_outcome(SimpleNamespace(id="d1", container=container), ApplyOutcome("f", True))
    return row

  async def test_it_restores_the_configuration_as_a_restart_would_find_it(self, manager, stores, backup, signals):
    generation, ran = Instance.get().state_generation, []

    async def waiting():
      ran.append(Worker.select().count())

    manager.add_task(task=waiting, key=("old",), scopes=set(), executor="main")
    manager.request_platform_restore(backup.id)
    await manager.run()
    assert [worker.hostname for worker in Worker.select()] == ["w1"]
    assert Notification.select().count() == 0 and Instance.get().state_generation != generation
    assert Settings().get("s3") == STORE_B and stores.installed[-1] == STORE_B
    assert (Containers().target("c"), Workers().setup("w1"), Domains().outcome("d1")) == (TargetRead("unobserved"), None, None)
    assert (manager.setup_failures, manager.stack_repairs, manager.removal_failures, manager.recoveries, manager.orphans) == (
        {}, {}, {}, {}, {})
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
    await take(manager)
    row = Backup.get()
    container = Container.create(application=application, worker=worker)
    ContainerEvent.create(container=container, subject_id=container.id, action="deploy", transition="admitted",
                          application_name="demo-web", worker_hostname="w1")
    manager.request_platform_restore(row.id)
    await manager.run()
    assert (ContainerEvent.select().count(), Notification.select().count(), Backup.select().count()) == (0, 0, 0)

  async def test_one_with_nothing_to_migrate_does_not_tell_of_the_starts_migration_again(self, manager, stores, backup):
    record("boot", "boot", "migrated", [], [{"where": "Sage", "command": None, "why": "x"}])
    manager.request_platform_restore(backup.id)
    await manager.run()
    assert manager.notices[-1] == (f"Platform restored from the backup taken at {taken_at(backup.s3_path)}.", "success", "/backups")


class TestMigrationNotice:
  def test_a_run_that_left_steps_says_how_many_and_links_to_its_log(self, manager):
    record("r1", "boot", "migrated", [], [{"where": "Sage", "command": None, "why": "x"}] * 3)
    manager.notify_migration("r1")
    assert manager.notices == [(f"This start of Sage brought its data up to v{config.VERSION}. It left 3 steps to do by hand.",
                                "warning", "/upgrade?migration=r1")]

  def test_a_restores_run_that_left_none_says_so(self, manager):
    record("r1", "restore", "recreated", [])
    manager.notify_migration("r1")
    assert manager.notices == [(f"The platform restore brought its data up to v{config.VERSION}.", "info",
                                "/upgrade?migration=r1")]

  def test_a_run_this_process_did_not_record_last_is_not_told(self, manager):
    record("r1", "boot", "migrated", [])
    manager.notify_migration("r2")
    record("r2", "restore", "migrated", [])
    manager.notify_migration("r1")
    assert manager.notices == []


class TestFacts:
  async def test_the_backup_status_follows_a_running_platform_backup_and_a_discovery_queued(self, manager):
    assert manager.backup_status() == {"platform_backup_in_progress": False, "discovery_in_progress": False}
    manager.add_task(task=asyncio.sleep, key=("backup_platform",), scopes=set(), executor="main", params={"delay": 0})
    manager.add_task(task=asyncio.sleep, key=("discover_backups",), scopes={"backup:store:sa"}, executor="main",
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
    backup = Backup.create(s3_path="base/backups/platform/20260101_030000_4444444444444444_0.2.0_1.db.enc", storage_id="sa", type="platform")
    manager.stop_tasks()
    with pytest.raises(Conflict, match="not running"):
      manager.request_platform_restore(backup.id)

  def test_its_facts_are_the_restore_and_the_backup_status(self, manager):
    facts = manager.platform_queue_facts()
    assert [fact._compute() for fact in facts] == [None, manager.backup_status()]


STARTUP = ["request_worker_observation", "request_worker_paths", "request_tunnel_listing", "request_container_observation",
           "request_certificate_renewal", "request_domain_reconciliation", "request_backup_discovery",
           "request_settings_checks"]


def queued(manager):
  return [(tuple(task["key"]), task["scopes"], task["executor"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]]


class TestStartup:
  def test_startup_requests_what_runs_when_sage_starts_and_checks_for_a_release_quietly(self, manager):
    manager.request_startup()
    assert manager.requested == STARTUP
    assert queued(manager) == [(("refresh_latest_release",), [], "main", True)]

  def test_a_release_check_asked_for_while_one_is_queued_is_that_one_made_visible(self, manager):
    queued_id = manager.request_release_check()
    assert manager.request_release_check(quiet=False) == queued_id
    assert queued(manager) == [(("refresh_latest_release",), [], "main", False)]

  async def test_a_release_check_asked_for_while_one_runs_is_that_one_made_visible(self, manager, register):
    register(Releases, SimpleNamespace(refresh=lambda: None))
    running_id = manager.request_release_check()
    manager.task_queue.dispatch_tick()
    assert manager.request_release_check(quiet=False) == running_id
    assert queued(manager) == []
    assert [task["quiet"] for task in manager.task_queue.snapshot()["running"]] == [False]
    await manager.run()


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
    assert queued(manager) == [(("flush_metrics",), ["metrics"], "main", True)]

  async def test_it_waits_while_the_daily_metrics_cleanup_runs(self, manager, flushed):
    manager.add_task(task=self.holding, key=("metrics_cleanup",), scopes={"metrics"}, executor="main")
    manager.task_queue.dispatch_tick()
    manager.request_metrics_flush()
    manager.task_queue.dispatch_tick()
    assert [name for (name,), *_ in queued(manager)] == ["flush_metrics"]
    await manager.run()
    assert flushed == [True]

  async def test_it_runs_while_other_work_holds_its_scopes(self, manager, flushed):
    manager.add_task(task=self.holding, key=("held",), scopes={"worker", "domain", "backup", "settings"}, executor="main")
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
    assert sorted(queued(manager)) == [(("logs_cleanup",), [], "common", True), (("metrics_cleanup",), ["metrics"], "main", True),
                                       (("purge_history",), [], "common", True), (("safety_copies_cleanup",), [], "common", True)]
    assert manager.requested == ["request_worker_tmp_cleanup", "request_backup_retention"] * 2

  async def test_a_cleanup_asked_for_while_one_runs_queues_none_of_its_parts(self, manager, cleaned):
    manager.request_cleanup()
    manager.task_queue.dispatch_tick()
    manager.request_cleanup()
    assert queued(manager) == []
    await manager.run()

  async def test_the_tasks_run_while_other_work_holds_its_scopes(self, manager, cleaned, clock):
    Notification.create(content="Deployed.")
    clock.advance(timedelta(days=15))

    async def holding():
      await asyncio.sleep(0.05)

    manager.add_task(task=holding, key=("held",), scopes={"worker", "domain", "backup", "settings"}, executor="main")
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
      self.task_queue = TaskQueue(SCOPES, names=TASK_NAMES | {"held", "later", "old"}, executors={}, record=lambda task, status: None)

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
        "start_time": "2026-01-01T09:00:00", "state": "running", "restoring_from": None, "migration": None}
    assert set(info["settings"]) == {"tailscale", "cloudflare", "s3", "notifications"}

  def test_it_shows_the_latest_migration_run_of_this_process(self, shown, data_dir):
    record("r1", "boot", "migrated", [], [{"where": "Sage", "command": None, "why": "x"}])
    assert shown.info()["migration"] == {"run": "r1", "trigger": "boot", "release": config.VERSION, "outcome": "migrated",
                                         "actions": 1}

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
