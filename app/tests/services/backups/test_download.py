from types import SimpleNamespace

import pytest

import config
from services.backups.download import download_filename, download_link
from services.db.models import Backup, Volume
from services.s3 import S3
from utils.errors import Conflict

PLATFORM_KEY = "base/backups/platform/20260102_030405.db.enc"


class Session:
  """A store holding `keys`, which records the links it was asked for."""

  def __init__(self, identity="s1", keys=()):
    self.identity, self.keys, self.links = identity, set(keys), []

  def key_exists(self, s3_path, raw_path=False):
    return raw_path and s3_path in self.keys

  def create_presigned_download_url(self, s3_path, expires_in, raw_path=False, filename=None):
    self.links.append({"s3_path": s3_path, "expires_in": expires_in, "raw_path": raw_path, "filename": filename})
    return "https://store.example.test/link"


@pytest.fixture
def volume(application):
  return Volume.create(application=application, name="data", path="/data")


def archive(volume, name="data"):
  return Backup.create(type="application", s3_path=f"base/backups/applications/{volume.id}/c1/20260102_030405.tar.gz.enc",
                       storage_id="s1", source_volume_id=volume.id, source_volume_name=name, application=volume.application_id)


class TestFilename:
  def test_a_platform_backup_keeps_its_file_name(self, db):
    assert download_filename(Backup(type="platform", s3_path=PLATFORM_KEY)) == "20260102_030405.db.enc"

  def test_a_volume_archive_says_its_application_its_volume_now_and_its_time(self, volume):
    backup = archive(volume, name="old-name")
    assert download_filename(backup) == "demo-web-data-20260102_030405.tar.gz.enc"

  def test_an_archive_of_a_volume_gone_says_the_name_it_had(self, volume):
    backup = archive(volume, name="old-name")
    volume.delete_instance()
    assert download_filename(Backup.get_by_id(backup.id)) == "demo-web-old-name-20260102_030405.tar.gz.enc"

  @pytest.mark.parametrize("key, name", [
      ("backups/platform/my backup (1).db.enc", "my_backup__1_.db.enc"), ('backups/platform/a"b.enc', "a_b.enc"),
      ("backups/platform/a;b\\c.enc", "a_b_c.enc"), ("backups/platform/café.enc", "caf_.enc"),
      ("backups/platform/ok-name_1.2.enc", "ok-name_1.2.enc")])
  def test_only_letters_digits_dots_dashes_and_underscores_are_kept(self, db, key, name):
    assert download_filename(Backup(type="platform", s3_path=key)) == name


class TestLink:
  def test_a_link_to_the_file_as_stored_under_its_name_for_the_browsers_time(self, register, db):
    store = Session(keys={PLATFORM_KEY})
    register(S3, SimpleNamespace(session=lambda: store))
    backup = Backup.create(type="platform", s3_path=PLATFORM_KEY, storage_id="s1")
    assert download_link(backup) == {"url": "https://store.example.test/link", "expires_in": config.BROWSER_LINK_SECONDS}
    assert store.links == [{"s3_path": PLATFORM_KEY, "expires_in": config.BROWSER_LINK_SECONDS, "raw_path": True,
                            "filename": "20260102_030405.db.enc"}]

  def test_a_backup_in_another_store_is_refused_and_one_whose_file_is_gone_not_found(self, register, db):
    store = Session(keys={PLATFORM_KEY})
    register(S3, SimpleNamespace(session=lambda: store))
    with pytest.raises(Conflict):
      download_link(Backup.create(type="platform", s3_path=PLATFORM_KEY, storage_id="s2"))
    with pytest.raises(LookupError):
      download_link(Backup.create(type="platform", s3_path="base/backups/platform/20260101_000000.db.enc", storage_id="s1"))
    assert store.links == []
