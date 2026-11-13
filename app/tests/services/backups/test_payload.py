from datetime import datetime
from types import SimpleNamespace

import pytest

from services.backups.payload import platform_backups, volume_backups
from services.db.models import Backup, Volume
from services.s3 import S3
from utils.errors import Conflict


@pytest.fixture(autouse=True)
def store(register):
  """The configured store, `store.identity`; None for none."""
  store = SimpleNamespace(identity="store-a")

  def session():
    if store.identity is None:
      raise Conflict("S3 is not configured.")
    return SimpleNamespace(identity=store.identity)

  register(S3, SimpleNamespace(session=session))
  return store


def backup(created_at, storage_id="store-a", **fields):
  return Backup.create(s3_path=f"k{created_at.day}", storage_id=storage_id, created_at=created_at, **fields)


def test_the_platforms_backups_are_listed_newest_first_without_the_volumes(application):
  old, new = backup(datetime(2026, 1, 1), type="platform"), backup(datetime(2026, 1, 3), type="platform")
  backup(datetime(2026, 1, 2), type="application", source_volume_id="v1", application=application)
  assert [item["id"] for item in platform_backups()] == [new.id, old.id]


def test_a_volumes_backups_are_found_by_its_id_whatever_it_was_named(application):
  volume = Volume.create(application=application, name="data", path="/data")
  before_rename = backup(datetime(2026, 1, 1), type="application", source_volume_id=volume.id, source_volume_name="old",
                         application=application)
  after = backup(datetime(2026, 1, 2), type="application", source_volume_id=volume.id, source_volume_name="data",
                 application=application)
  backup(datetime(2026, 1, 3), type="application", source_volume_id="another", source_volume_name="data", application=application)
  items = volume_backups(volume.id)
  assert [(item["id"], item["source_volume_name"]) for item in items] == [(after.id, "data"), (before_rename.id, "old")]
  assert items[0]["storage_id"] == "store-a" and items[0]["application"] == application.id


def test_only_the_configured_stores_backups_are_listed_and_none_without_a_store(application, store):
  volume = Volume.create(application=application, name="data", path="/data")
  here = [backup(datetime(2026, 1, 1), type="platform"),
          backup(datetime(2026, 1, 2), type="application", source_volume_id=volume.id, application=application)]
  backup(datetime(2026, 1, 3), "store-b", type="platform")
  backup(datetime(2026, 1, 4), "store-b", type="application", source_volume_id=volume.id, application=application)
  assert [item["id"] for item in (*platform_backups(), *volume_backups(volume.id))] == [backup.id for backup in here]
  store.identity = None
  assert platform_backups() == [] and volume_backups(volume.id) == []
