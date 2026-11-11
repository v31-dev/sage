from datetime import datetime

from services.backups.payload import platform_backups, volume_backups
from services.db.models import Backup, Volume


def backup(created_at, **fields):
  return Backup.create(s3_path=f"k{created_at.day}", storage_id="store-a", created_at=created_at, **fields)


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
