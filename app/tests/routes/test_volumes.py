from types import SimpleNamespace

import pytest
from fastapi.encoders import jsonable_encoder

from services.applications.payload import volume_payload
from services.db.models import Application, Backup, Container, Volume, Worker
from services.s3 import S3
from tests.services.backups.test_download import Session

URL = "/api/projects/demo/applications/web/volumes"


@pytest.fixture
def volume(application):
  return Volume.create(application=application, name="data", path="/data")


@pytest.fixture
def other(application):
  """Another application of the project, with a volume of the same name."""
  other = Application.create(project=application.project, name="api", label="Api")
  return Volume.create(application=other, name="data", path="/data")


def archive(volume, storage_id="s1"):
  return Backup.create(type="application", s3_path=f"base/backups/applications/{volume.id}/c1/20260102_030405_4444444444444444.tar.gz.enc",
                       storage_id=storage_id, source_volume_id=volume.id, source_volume_name=volume.name,
                       application=volume.application_id)


class TestVolumes:
  def test_a_volume_is_created_for_the_application_and_answered_with_its_payload(self, client, application):
    response = client.post(f"{URL}/", json={"name": "data", "path": "/data", "backup_cron": "0 3 * * *"})
    volume = Volume.get(Volume.application == application.id)
    assert response.json() == jsonable_encoder(volume_payload(volume))
    assert (volume.name, volume.path, volume.backup_cron) == ("data", "/data", "0 3 * * *")

  def test_what_the_command_refuses_is_its_status(self, client, volume):
    assert client.post("/api/projects/demo/applications/missing/volumes/", json={"name": "x", "path": "/x"}).status_code == 404
    assert client.post(f"{URL}/", json={"name": "data", "path": "/other"}).status_code == 409
    assert client.post(f"{URL}/", json={"name": "logs", "path": "/logs", "backup_cron": "often"}).status_code == 400
    assert Volume.select().count() == 1

  def test_an_edit_is_written_and_answered_with_its_payload(self, client, volume):
    response = client.put(f"{URL}/data", json={"backup_cron": ""})
    assert (response.json()["backup_cron"], Volume.get_by_id(volume.id).backup_cron) == (None, None)

  def test_a_volume_of_another_application_or_none_is_a_404(self, client, volume, other):
    assert client.put(f"{URL}/logs", json={"path": "/x"}).status_code == 404
    assert client.put("/api/projects/demo/applications/api/volumes/data", json={"path": "/moved"}).status_code == 200
    assert (Volume.get_by_id(volume.id).path, Volume.get_by_id(other.id).path) == ("/data", "/moved")

  def test_a_volume_is_deleted_with_no_content(self, client, volume):
    assert client.delete(f"{URL}/data").status_code == 204
    assert Volume.select().count() == 0


class TestBackups:
  def test_the_list_is_this_volumes_backups(self, client, volume, other, register):
    register(S3, SimpleNamespace(session=Session))
    own = archive(volume)
    archive(other)
    assert [backup["id"] for backup in client.get(f"{URL}/data/backups/").json()] == [own.id]

  def test_a_backup_asks_for_one_from_each_container_and_answers_the_tasks(self, client, application, volume, manager):
    manager.answers["request_backup"] = ["t1"]
    assert client.post(f"{URL}/data/backups/").json() == {"task_ids": ["t1"]}
    assert manager.called("request_backup") == [((application.id, [volume.id]), {})]

  def test_a_download_is_a_link_to_this_volumes_backup(self, client, volume, register):
    backup = archive(volume)
    store = Session(keys={backup.s3_path})
    register(S3, SimpleNamespace(session=lambda: store))
    response = client.get(f"{URL}/data/backups/{backup.id}/download")
    assert response.json()["url"] == "https://store.example.test/link"
    assert [link["s3_path"] for link in store.links] == [backup.s3_path]

  def test_a_backup_of_another_volume_or_the_platform_is_not_found_here(self, client, volume, other, register):
    elsewhere = archive(other)
    platform = Backup.create(type="platform", s3_path="base/backups/platform/20260102_030405_4444444444444444.db.enc", storage_id="s1")
    store = Session(keys={elsewhere.s3_path, platform.s3_path})
    register(S3, SimpleNamespace(session=lambda: store))
    for backup in (elsewhere, platform):
      assert client.get(f"{URL}/data/backups/{backup.id}/download").status_code == 404
    assert store.links == []


class TestRestore:
  @pytest.fixture
  def container(self, application, worker):
    return Container.create(application=application, worker=worker)

  def test_a_backup_is_restored_into_the_container_named_and_its_own_volume(self, client, volume, container, manager):
    backup = archive(volume)
    response = client.post(f"{URL}/data/backups/{backup.id}/restore", json={"container": container.id})
    assert response.json() == {"task_id": "task-1"}
    assert manager.called("request_restore") == [((container.id, backup.id, volume.id), {})]

  def test_another_volume_of_the_application_can_be_named(self, client, application, volume, container, manager):
    logs = Volume.create(application=application, name="logs", path="/logs")
    backup = archive(volume)
    client.post(f"{URL}/data/backups/{backup.id}/restore", json={"container": container.id, "volume": "logs"})
    assert manager.called("request_restore") == [((container.id, backup.id, logs.id), {})]

  def test_a_container_or_volume_not_of_the_application_or_none_is_a_404_and_asks_for_nothing(
          self, client, volume, other, container, manager):
    backup = archive(volume)
    elsewhere = Container.create(application=other.application, worker=Worker.create(hostname="w2", subdomain="blue"))
    for body in ({"container": elsewhere.id}, {}, {"container": container.id, "volume": "missing"}):
      assert client.post(f"{URL}/data/backups/{backup.id}/restore", json=body).status_code == 404
    assert client.post(f"{URL}/data/backups/{archive(other).id}/restore", json={"container": container.id}).status_code == 404
    assert manager.calls == []
