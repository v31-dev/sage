import pytest

import config
from routes.backups import download_filename
from services.db import Application, Backup, Container, Volume
from utils.queue import OnConflict

PLATFORM_PATH = "backups/platform/20260102_030405.db.enc"
VOLUME_PATH = "backups/applications/demo/web/w1/data/20260102_030405.tar.gz.enc"
PLATFORM = "/api/backups"
VOLUME = "/api/projects/demo/applications/web/volumes/data/backups"


@pytest.fixture
def platform_backup(db):
  return Backup.create(type="platform", s3_path=PLATFORM_PATH)


@pytest.fixture
def volume_backup(application, volume):
  return Backup.create(type="application", s3_path=VOLUME_PATH, application=application, source_volume_name="data")


@pytest.fixture
def with_container(application, worker):
  return Container.create(application=application, worker=worker)


class TestDownloadFilename:
  @pytest.mark.parametrize("kind, path, name", [
      ("platform", PLATFORM_PATH, "20260102_030405.db.enc"),
      ("platform", "/backups/platform/20260102_030405.db.enc/", "20260102_030405.db.enc"),
      ("application", VOLUME_PATH, "demo-web-w1-data-20260102_030405.tar.gz.enc"),
      ("application", "bucket/" + VOLUME_PATH, "demo-web-w1-data-20260102_030405.tar.gz.enc"),
      ("application", "a/b.enc", "a-b.enc"),
  ])
  def test_a_platform_backup_is_its_file_name_and_a_volume_backup_says_which_volume(self, kind, path, name):
    assert download_filename(Backup(type=kind, s3_path=path)) == name

  @pytest.mark.parametrize("path, name", [
      ("backups/platform/my backup (1).db.enc", "my_backup__1_.db.enc"),
      ('backups/platform/a"b.enc', "a_b.enc"),
      ("backups/platform/a;b\\c.enc", "a_b_c.enc"),
      ("backups/platform/café.enc", "caf_.enc"),
      ("backups/platform/line\nbreak.enc", "line_break.enc"),
      ("backups/platform/ok-name_1.2.enc", "ok-name_1.2.enc"),
  ])
  def test_only_letters_digits_and_dot_underscore_dash_are_kept_because_the_name_is_put_in_quotes(self, path, name):
    assert download_filename(Backup(type="platform", s3_path=path)) == name


class TestWithoutS3:
  @pytest.fixture(autouse=True)
  def off(self, s3):
    s3.s3_config = None

  @pytest.mark.parametrize("method, url", [
      ("post", f"{PLATFORM}/"),
      ("post", f"{PLATFORM}/discover"),
      ("get", f"{PLATFORM}/1/download"),
      ("delete", f"{PLATFORM}/1"),
      ("post", f"{PLATFORM}/1/restore"),
  ])
  def test_every_backup_action_is_a_400_that_points_to_the_settings(self, client, manager, platform_backup, method, url):
    response = getattr(client, method)(url)

    assert response.status_code == 400
    assert response.json()["detail"] == "S3 is not configured. Set it up in Settings first."
    assert manager.tasks == []

  def test_listing_still_works(self, client, platform_backup):
    assert len(client.get(f"{PLATFORM}/").json()) == 1


class TestList:
  def test_platform_backups_only_at_the_top_level(self, client, platform_backup, volume_backup):
    assert [row["s3_path"] for row in client.get(f"{PLATFORM}/").json()] == [PLATFORM_PATH]

  def test_a_volumes_own_backups_under_the_volume(self, client, platform_backup, volume_backup, application):
    Backup.create(type="application", s3_path="backups/applications/demo/web/w1/other/x.tar.gz.enc", application=application,
                  source_volume_name="other")

    assert [row["s3_path"] for row in client.get(f"{VOLUME}/").json()] == [VOLUME_PATH]

  def test_nothing_listed_when_there_are_none(self, client, db):
    assert client.get(f"{PLATFORM}/").json() == []


class TestInjectBackup:
  def test_an_unknown_backup_is_a_404(self, client, db):
    response = client.get(f"{PLATFORM}/99/download")
    assert (response.status_code, response.json()["detail"]) == (404, "Backup not found")

  def test_a_platform_backup_is_not_found_under_a_volume(self, client, platform_backup, volume):
    assert client.get(f"{VOLUME}/{platform_backup.id}/download").status_code == 404

  def test_a_backup_of_another_volume_is_not_found_even_if_its_file_exists(self, client, s3, volume_backup, application, volume):
    s3.keys.add(VOLUME_PATH)
    Volume.create(application=application, name="other", path="/other")

    response = client.get(f"/api/projects/demo/applications/web/volumes/other/backups/{volume_backup.id}/download")

    assert (response.status_code, response.json()["detail"]) == (404, "Backup not found")

  def test_a_backup_of_another_application_is_not_found_even_if_its_file_exists(self, client, s3, volume_backup):
    s3.keys.add(VOLUME_PATH)
    other = Application.create(project=volume_backup.application.project, name="api", label="Api")
    Volume.create(application=other, name="data", path="/data")

    response = client.get(f"/api/projects/demo/applications/api/volumes/data/backups/{volume_backup.id}/download")

    assert (response.status_code, response.json()["detail"]) == (404, "Backup not found")


class TestStatus:
  def test_neither_running_nor_discovering(self, client, manager):
    assert client.get(f"{PLATFORM}/platform_backup_status").json() == {"platform_backup_in_progress": False, "discovery_in_progress": False}

  def test_each_flag_follows_its_task(self, client, manager):
    manager.running.add("backup_database_s3")
    manager.queued.add("discover_s3_backups")

    assert client.get(f"{PLATFORM}/platform_backup_status").json() == {"platform_backup_in_progress": True, "discovery_in_progress": True}


class TestCreatePlatformBackup:
  def test_it_queues_the_backup_replacing_a_pending_one(self, client, manager):
    response = client.post(f"{PLATFORM}/")

    assert response.json() == {"status": "OK"}
    [queued] = manager.tasks
    assert queued["task"].__name__ == "backup_database_s3"
    assert queued["scopes"] == {"platform", "app"}
    assert queued["executor"] == "platform"
    assert queued["on_conflict"] is OnConflict.REPLACE
    assert queued["task_id"] == response.headers["X-Task-ID"]


class TestCreateVolumeBackup:
  def test_it_queues_the_backup_of_that_volume_for_the_application(self, client, manager, volume, with_container):
    response = client.post(f"{VOLUME}/")

    assert response.json() == {"status": "OK"}
    [queued] = manager.tasks
    assert queued["task"].__name__ == "backup_application_s3"
    assert queued["scopes"] == {"app:demo-web"}
    assert queued["params"] == {"application_id": volume.application_id, "volume_ids": [volume.id]}
    assert queued["executor"] == "app"
    assert queued["task_id"] == response.headers["X-Task-ID"]

  @pytest.mark.parametrize("status", ["active", "error", "deploying", "backup"])
  def test_only_an_inactive_application_can_be_backed_up_by_hand(self, client, manager, application, volume, with_container, status):
    application.status = status
    application.save()

    response = client.post(f"{VOLUME}/")

    assert response.status_code == 409
    assert "only allowed when the application is inactive" in response.json()["detail"]
    assert manager.tasks == []

  def test_an_application_without_containers_has_nothing_to_back_up(self, client, manager, volume):
    response = client.post(f"{VOLUME}/")

    assert response.status_code == 400
    assert response.json()["detail"] == "Application demo-web has no containers to back up."
    assert manager.tasks == []

  def test_a_refused_task_is_a_409(self, client, manager, volume, with_container):
    manager.admits = False

    response = client.post(f"{VOLUME}/")

    assert response.status_code == 409
    assert response.json()["detail"] == "Application already has an operation in progress."


class TestDiscover:
  def test_it_queues_the_discovery(self, client, manager):
    response = client.post(f"{PLATFORM}/discover")

    assert response.json() == {"status": "OK"}
    [queued] = manager.tasks
    assert queued["task"].__name__ == "discover_s3_backups"
    assert queued["scopes"] == {"platform", "app"}
    assert queued["executor"] == "platform"
    assert queued["task_id"] == response.headers["X-Task-ID"]


class TestDownload:
  def test_it_returns_a_short_lived_link_named_after_the_backup(self, client, s3, platform_backup):
    s3.keys.add(PLATFORM_PATH)

    response = client.get(f"{PLATFORM}/{platform_backup.id}/download")

    assert response.status_code == 200
    assert response.json() == {"url": f"https://s3.example.test/{PLATFORM_PATH}", "expires_in": config.BROWSER_LINK_SECONDS}
    assert s3.links == [{"s3_path": PLATFORM_PATH, "expires_in": config.BROWSER_LINK_SECONDS, "raw_path": True,
                         "filename": "20260102_030405.db.enc"}]

  def test_the_stored_path_is_used_as_it_is_not_joined_to_the_base_path(self, client, s3, platform_backup):
    s3.keys.add(PLATFORM_PATH)

    client.get(f"{PLATFORM}/{platform_backup.id}/download")

    assert s3.checked == [{"s3_path": PLATFORM_PATH, "raw_path": True}]
    assert s3.links[0]["raw_path"] is True

  def test_a_volume_backup_is_named_after_its_volume(self, client, s3, volume_backup):
    s3.keys.add(VOLUME_PATH)

    client.get(f"{VOLUME}/{volume_backup.id}/download")

    assert s3.links[0]["filename"] == "demo-web-w1-data-20260102_030405.tar.gz.enc"

  def test_a_file_that_is_gone_from_s3_is_a_404_and_no_link_is_made(self, client, s3, platform_backup):
    response = client.get(f"{PLATFORM}/{platform_backup.id}/download")

    assert response.status_code == 404
    assert response.json()["detail"] == "The backup file is no longer in S3."
    assert s3.links == []


class TestDelete:
  def test_the_record_goes_and_the_file_is_queued_for_deletion(self, client, manager, platform_backup):
    response = client.delete(f"{PLATFORM}/{platform_backup.id}")

    assert response.json() == {"status": "OK"}
    assert Backup.select().count() == 0
    [queued] = manager.tasks
    assert queued["task"].__name__ == "delete_backup_s3"
    assert queued["params"] == {"s3_path": PLATFORM_PATH}
    assert queued["scopes"] == {"common"}
    assert queued["executor"] == "common"
    assert queued["on_conflict"] is OnConflict.QUEUE
    assert queued["task_id"] == response.headers["X-Task-ID"]

  def test_a_volume_backup_is_deleted_the_same_way(self, client, manager, volume_backup):
    client.delete(f"{VOLUME}/{volume_backup.id}")

    assert Backup.select().count() == 0
    assert manager.tasks[0]["params"] == {"s3_path": VOLUME_PATH}

  def test_an_unknown_backup_queues_nothing(self, client, manager, db):
    assert client.delete(f"{PLATFORM}/99").status_code == 404
    assert manager.tasks == []


class TestRestorePlatform:
  def test_it_queues_the_restore_that_blocks_sage_until_it_is_done(self, client, manager, platform_backup):
    response = client.post(f"{PLATFORM}/{platform_backup.id}/restore")

    assert response.json() == {"status": "OK"}
    [queued] = manager.tasks
    assert queued["task"].__name__ == "restore_database_from_s3"
    assert queued["params"] == {"s3_path": PLATFORM_PATH}
    assert queued["scopes"] == {"platform", "app"}
    assert queued["executor"] == "platform"
    assert queued["restore"] is True
    assert queued["task_id"] == response.headers["X-Task-ID"]

  def test_a_refused_task_is_a_409(self, client, manager, platform_backup):
    manager.admits = False

    response = client.post(f"{PLATFORM}/{platform_backup.id}/restore")

    assert response.status_code == 409
    assert response.json()["detail"] == "A platform operation is already in progress."

  def test_a_backup_that_is_not_a_platform_backup_cannot_be_restored_this_way(self, client, manager, volume_backup):
    response = client.post(f"{PLATFORM}/{volume_backup.id}/restore")

    assert response.status_code == 400
    assert response.json()["detail"] == "Only platform backups can be restored"
    assert manager.tasks == []


class TestRestoreVolume:
  def test_it_queues_the_restore_onto_the_chosen_worker(self, client, manager, platform_backup, volume_backup):
    response = client.post(f"{VOLUME}/{volume_backup.id}/restore", json={"target_worker": " w2 "})

    assert response.json() == {"status": "OK"}
    [queued] = manager.tasks
    assert queued["task"].__name__ == "restore_application_volume_from_s3"
    assert queued["params"] == {
        "application_id": volume_backup.application_id,
        "volume_id": volume_backup.application.volumes[0].id,
        "backup_id": volume_backup.id,
        "target_worker_hostname": "w2",
    }
    assert queued["scopes"] == {"app:demo-web"}
    assert queued["executor"] == "app"
    assert queued["task_id"] == response.headers["X-Task-ID"]

  @pytest.mark.parametrize("status", ["active", "error", "deploying", "restoring"])
  def test_only_an_inactive_application_can_be_restored_into(self, client, manager, application, volume_backup, status):
    application.status = status
    application.save()

    response = client.post(f"{VOLUME}/{volume_backup.id}/restore", json={"target_worker": "w1"})

    assert response.status_code == 409
    assert "only allowed when the application is inactive" in response.json()["detail"]
    assert manager.tasks == []

  @pytest.mark.parametrize("body", [None, {}, {"target_worker": ""}, {"target_worker": "   "}, {"target_worker": None}, {"target_worker": 5}])
  def test_a_target_worker_is_required(self, client, manager, volume_backup, body):
    response = client.post(f"{VOLUME}/{volume_backup.id}/restore", json=body)

    assert response.status_code == 400
    assert response.json()["detail"] == "target_worker is required for volume restore"
    assert manager.tasks == []

  def test_a_refused_task_is_a_409(self, client, manager, volume_backup):
    manager.admits = False

    response = client.post(f"{VOLUME}/{volume_backup.id}/restore", json={"target_worker": "w1"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Application already has an operation in progress."
