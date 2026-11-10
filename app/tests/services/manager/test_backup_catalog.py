import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services.backups.keys import BACKUP_TIMESTAMP_FORMAT, volume_archive_key
from services.db.models import Backup, Volume
from services.manager.backup_catalog import BackupCatalogMixin
from services.manager.queue import SCOPES, QueueMixin
from services.s3 import S3
from services.updates import UpdateSource
from utils.executor import active_executor
from utils.logging import TaskFailed
from utils.queue import TaskQueue

NOW = datetime(2026, 1, 20, 12)


class Store:
  """A store as a folder: its objects are files under it, at their full keys; `fails` names a prefix whose listing or
  deletion fails."""

  def __init__(self, root: Path, identity: str):
    self.root, self.identity, self.fails = root, identity, None
    self.target = SimpleNamespace(path="base")

  def get_full_path(self, key):
    return f"base/{key.strip('/')}"

  def put(self, key, full=False):
    path = self.root / (key if full else self.get_full_path(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("archive")
    return str(path.relative_to(self.root))

  def keys(self) -> set[str]:
    return {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}

  def _under(self, prefix):
    if self.fails and prefix.startswith(self.get_full_path(self.fails)):
      raise Exception("AccessDenied")
    return sorted(key for key in self.keys() if key.startswith(prefix))

  async def get_keys(self, prefix):
    return self._under(self.get_full_path(prefix))

  async def delete_key(self, s3_path, filter=None, raw_path=False):
    deleted = [key for key in self._under(s3_path.strip("/") if raw_path else self.get_full_path(s3_path))
               if filter is None or filter(key)]
    for key in deleted:
      (self.root / key).unlink()
    return deleted


class Manager(BackupCatalogMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=1)
    self.queue_facts = []
    self.task_queue = TaskQueue(SCOPES, {"common": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  async def run(self):
    for _ in range(500):
      if not (self.task_queue.snapshot()["running"] or self.task_queue.snapshot()["queued"]):
        return
      self.task_queue.dispatch_tick()
      await asyncio.sleep(0.01)


@pytest.fixture
def stores(tmp_path, register):
  """Two stores, A configured; `stores.configured` is the one S3 hands out, None for none."""
  stores = SimpleNamespace(a=Store(tmp_path / "a", "sa"), b=Store(tmp_path / "b", "sb"))
  stores.configured = stores.a

  def session():
    if stores.configured is None:
      raise Exception("S3 configuration is not set. Please set it in settings.")
    return stores.configured

  register(S3, SimpleNamespace(session=session))
  return stores


@pytest.fixture
def manager(stores, clock):
  clock.set(NOW)
  manager = Manager()
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def volume(application):
  return Volume.create(application=application, name="data", path="/data")


def stamp(days_ago=0):
  return (NOW - timedelta(days=days_ago)).strftime(BACKUP_TIMESTAMP_FORMAT)


def archive_key(volume, days_ago=0, container="0123456789abcdef"):
  return volume_archive_key(volume.id, container, stamp(days_ago))


def row(store, key, volume=None, **fields):
  return Backup.create(s3_path=store.get_full_path(key), storage_id=store.identity, type="application" if volume else "platform",
                       source_volume_id=volume.id if volume else None, source_volume_name=volume.name if volume else None,
                       application=volume.application_id if volume else None, **fields)


def catalog():
  return sorted((backup.storage_id, backup.s3_path, backup.type, backup.source_volume_name) for backup in Backup.select())


def queued(manager):
  return [(tuple(task["key"]), task["scopes"], task["params"], task["labels"], task["quiet"])
          for task in manager.task_queue.snapshot()["queued"]]


class TestDeletion:
  def test_the_row_goes_at_once_and_its_archives_deletion_holds_its_part_of_its_store(
          self, manager, stores, volume, db, signals):
    backup = row(stores.a, archive_key(volume), volume)
    manager.request_backup_deletion(backup.id)
    assert catalog() == [] and (UpdateSource.BACKUPS, None) in signals
    part = hashlib.sha256(backup.s3_path.encode()).hexdigest()[:16]
    assert queued(manager) == [(("delete_archive", backup.s3_path), [f"backup:store:sa:delete:{part}"],
                                {"s3_path": backup.s3_path, "storage_id": "sa"}, {"Backup of": "data"}, False)]

  def test_a_platform_backup_is_named_so(self, manager, stores, db):
    manager.request_backup_deletion(row(stores.a, f"backups/platform/{stamp()}.db.enc").id)
    assert queued(manager)[0][3] == {"Backup of": "the platform"}

  def test_a_backup_not_listed_is_not_found(self, manager, db):
    with pytest.raises(LookupError):
      manager.request_backup_deletion(999)

  async def test_exactly_its_archive_is_deleted(self, manager, stores, volume):
    key = stores.a.put(archive_key(volume))
    sibling = stores.a.put(key + ".old", full=True)
    await manager.delete_archive(key, "sa")
    assert stores.a.keys() == {sibling}

  async def test_a_deletion_queued_before_the_store_changed_touches_neither_store(self, manager, stores, volume):
    backup = row(stores.a, archive_key(volume), volume)
    stores.a.put(backup.s3_path, full=True)
    stores.b.put(backup.s3_path, full=True)
    manager.request_backup_deletion(backup.id)
    stores.configured = stores.b
    await manager.run()
    assert stores.a.keys() == {backup.s3_path} and stores.b.keys() == {backup.s3_path}

  async def test_a_deletion_that_fails_fails_the_task(self, manager, stores, volume):
    key = stores.a.put(archive_key(volume))
    stores.a.fails = "backups"
    with pytest.raises(TaskFailed):
      await manager.delete_archive(key, "sa")


class TestDiscovery:
  def test_it_holds_its_store_and_nothing_is_queued_without_one(self, manager, stores, db):
    assert manager.request_backup_discovery() is not None
    assert queued(manager) == [(("discover_backups",), ["backup:store:sa"], {"storage_id": "sa"}, {}, False)]
    stores.configured = None
    assert manager.request_backup_discovery() is None

  async def test_the_table_comes_to_mirror_the_store(self, manager, stores, volume, signals):
    platform = stores.a.put(f"backups/platform/{stamp(1)}.db.enc")
    archive = stores.a.put(archive_key(volume, 2))
    stores.a.put(volume_archive_key("fedcba9876543210", "0123456789abcdef", stamp()))
    kept = row(stores.a, archive_key(volume, 3), volume)
    stores.a.put(kept.s3_path, full=True)
    row(stores.a, archive_key(volume, 4), volume)
    row(stores.b, archive_key(volume, 5), volume)
    signals.clear()
    await manager.discover_backups("sa")
    assert catalog() == sorted([("sa", platform, "platform", None), ("sa", archive, "application", "data"),
                                ("sa", kept.s3_path, "application", "data")])
    added = Backup.get(Backup.s3_path == archive)
    assert (added.application_id, added.source_volume_id, added.created_at) == (volume.application_id, volume.id, NOW - timedelta(days=2))
    assert signals.count((UpdateSource.BACKUPS, None)) == 1
    signals.clear()
    await manager.discover_backups("sa")
    assert signals == []

  async def test_an_archive_being_deleted_is_not_listed_again(self, manager, stores, volume):
    backup = row(stores.a, archive_key(volume), volume)
    stores.a.put(backup.s3_path, full=True)
    manager.request_backup_deletion(backup.id)
    await manager.discover_backups("sa")
    assert catalog() == []

  async def test_nothing_is_done_once_the_store_changed(self, manager, stores, volume):
    stores.a.put(archive_key(volume))
    stores.b.put(archive_key(volume, 1))
    stores.configured = stores.b
    await manager.discover_backups("sa")
    assert catalog() == []

  async def test_a_request_while_one_runs_gets_one_after_it(self, manager, stores, volume):
    release = asyncio.Event()
    original = stores.a.get_keys

    async def get_keys(prefix):
      await release.wait()
      return await original(prefix)

    stores.a.get_keys = get_keys
    manager.request_backup_discovery()
    manager.task_queue.dispatch_tick()
    manager.request_backup_discovery()
    assert [len(manager.task_queue.snapshot()[state]) for state in ("running", "queued")] == [1, 1]
    release.set()
    await manager.run()

  async def test_a_listing_that_fails_leaves_the_table_as_it_is(self, manager, stores, volume):
    row(stores.a, archive_key(volume), volume)
    stores.a.fails = "backups/applications"
    with pytest.raises(TaskFailed):
      await manager.discover_backups("sa")
    assert len(catalog()) == 1


class TestRetention:
  def test_it_is_quiet_holds_its_store_and_is_not_queued_without_one(self, manager, stores, db):
    manager.request_backup_retention()
    assert queued(manager) == [(("prune_backups",), ["backup:store:sa"], {"storage_id": "sa"}, {}, True)]
    stores.configured = None
    manager.task_queue.cancel(("prune_backups",))
    manager.request_backup_retention()
    assert queued(manager) == []

  async def test_backups_past_the_retention_period_go_with_their_rows(self, manager, stores, volume):
    old = [row(stores.a, key, volume if "applications" in key else None)
           for key in (archive_key(volume, config.RETENTION_DAYS + 1), f"backups/platform/{stamp(config.RETENTION_DAYS + 2)}.db.enc")]
    recent = row(stores.a, archive_key(volume, config.RETENTION_DAYS - 1), volume)
    elsewhere = row(stores.b, archive_key(volume, config.RETENTION_DAYS + 1), volume)
    for backup in (*old, recent):
      stores.a.put(backup.s3_path, full=True)
    stores.b.put(elsewhere.s3_path, full=True)
    await manager.prune_backups("sa")
    assert stores.a.keys() == {recent.s3_path} and stores.b.keys() == {elsewhere.s3_path}
    assert catalog() == sorted([("sa", recent.s3_path, "application", "data"), ("sb", elsewhere.s3_path, "application", "data")])

  @pytest.mark.parametrize("failing", ["backups/platform", "backups/applications"])
  async def test_a_prefix_that_fails_still_lets_the_other_go_and_fails_the_task(self, manager, stores, volume, failing):
    platform = row(stores.a, f"backups/platform/{stamp(config.RETENTION_DAYS + 1)}.db.enc")
    archive = row(stores.a, archive_key(volume, config.RETENTION_DAYS + 1), volume)
    for backup in (platform, archive):
      stores.a.put(backup.s3_path, full=True)
    stores.a.fails = failing
    with pytest.raises(TaskFailed):
      await manager.prune_backups("sa")
    assert catalog() == [next(entry for entry in [("sa", platform.s3_path, "platform", None), ("sa", archive.s3_path, "application", "data")]
                              if failing.split("/")[1] in entry[1])]

  async def test_nothing_is_done_once_the_store_changed(self, manager, stores, volume):
    old = row(stores.a, archive_key(volume, config.RETENTION_DAYS + 1), volume)
    stores.a.put(old.s3_path, full=True)
    stores.b.put(old.s3_path, full=True)
    stores.configured = stores.b
    await manager.prune_backups("sa")
    assert stores.a.keys() == {old.s3_path} and stores.b.keys() == {old.s3_path}


class TestScopes:
  async def test_a_discovery_running_holds_back_the_stores_deletions_and_retention_and_not_another_stores(
          self, manager, stores, volume):
    release = asyncio.Event()

    async def held():
      await release.wait()

    manager.add_task(task=held, key=("discover_backups",), scopes={"backup:store:sa"}, executor="common")
    manager.task_queue.dispatch_tick()
    here = row(stores.a, archive_key(volume), volume)
    there = row(stores.b, archive_key(volume, 1), volume)
    manager.request_backup_deletion(here.id)
    manager.request_backup_deletion(there.id)
    manager.request_backup_retention()
    manager.task_queue.dispatch_tick()
    assert sorted(task["key"][0] for task in manager.task_queue.snapshot()["queued"]) == ["delete_archive", "prune_backups"]
    assert [task["params"]["storage_id"] for task in manager.task_queue.snapshot()["queued"] if task["key"][0] == "delete_archive"] == ["sa"]
    release.set()
    await manager.run()
