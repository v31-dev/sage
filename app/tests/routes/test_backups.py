from types import SimpleNamespace

import pytest

from services.db.models import Backup, Volume
from services.s3 import S3
from tests.services.backups.test_download import Session
from utils.errors import Conflict

URL = "/api/backups"
PLATFORM_KEY = "base/backups/platform/20260102_030405.db.enc"


@pytest.fixture
def platform(db):
  return Backup.create(type="platform", s3_path=PLATFORM_KEY, storage_id="s1")


@pytest.fixture
def archive(application):
  volume = Volume.create(application=application, name="data", path="/data")
  return Backup.create(type="application", s3_path=f"base/backups/applications/{volume.id}/c1/20260102_030405.tar.gz.enc",
                       storage_id="s1", source_volume_id=volume.id, source_volume_name="data", application=application)


@pytest.fixture
def store(register, archive):
  store = Session(keys={PLATFORM_KEY, archive.s3_path})
  register(S3, SimpleNamespace(session=lambda: store))
  return store


@pytest.fixture
def no_store(register):
  def refuse():
    raise Conflict("S3 is not configured.")
  register(S3, SimpleNamespace(session=refuse))


class TestReads:
  def test_the_list_is_the_platforms_backups_only(self, client, platform, archive):
    assert [backup["id"] for backup in client.get(f"{URL}/").json()] == [platform.id]

  def test_the_status_is_the_managers(self, client, manager):
    client.get(f"{URL}/platform_backup_status")
    assert manager.called("backup_status") == [((), {})]


class TestBackupAndDiscovery:
  @pytest.mark.parametrize("path, request_name", [("/", "request_platform_backup"), ("/discover", "request_backup_discovery")])
  def test_each_answers_its_task(self, client, store, manager, path, request_name):
    manager.answers[request_name] = "t1"
    assert client.post(f"{URL}{path}").json() == {"task_id": "t1"}
    assert manager.called(request_name) == [((), {})]

  @pytest.mark.parametrize("path", ["/", "/discover"])
  def test_without_s3_each_is_refused_and_asks_for_nothing(self, client, no_store, manager, path):
    response = client.post(f"{URL}{path}")
    assert (response.status_code, response.json()["detail"]) == (409, "S3 is not configured.")
    assert manager.calls == []


class TestOneBackup:
  def test_a_download_is_a_link_to_the_platform_backup(self, client, platform, store):
    assert client.get(f"{URL}/{platform.id}/download").json()["url"] == "https://store.example.test/link"
    assert [link["s3_path"] for link in store.links] == [PLATFORM_KEY]

  def test_a_delete_asks_for_the_backups_deletion(self, client, platform, manager):
    assert client.delete(f"{URL}/{platform.id}").json() == {"task_id": "task-1"}
    assert manager.called("request_backup_deletion") == [((platform.id,), {})]

  def test_a_restore_asks_for_the_backup_named(self, client, platform, manager):
    assert client.post(f"{URL}/{platform.id}/restore").json() == {"task_id": "task-1"}
    assert manager.called("request_platform_restore") == [((platform.id,), {})]

  def test_a_volumes_backup_or_none_is_not_found_here_and_asks_for_nothing(self, client, archive, store, manager):
    for backup in (archive.id, 999):
      assert client.delete(f"{URL}/{backup}").status_code == 404
      assert client.get(f"{URL}/{backup}/download").status_code == 404
    assert manager.calls == [] and store.links == []
    assert Backup.select().count() == 1
