import pytest
from fastapi import HTTPException

from routes.volumes import validate_backup_cron
from services.db import Application, Volume

URL = "/api/projects/demo/applications/web/volumes"


class TestValidateBackupCron:
  @pytest.mark.parametrize("expression", [None, "", "   "])
  def test_no_schedule_is_none(self, expression):
    assert validate_backup_cron(expression) is None

  @pytest.mark.parametrize("expression", ["0 3 * * *", "*/5 * * * *", "0 0 1 * 1", "30 2 * * 0,6"])
  def test_a_valid_five_part_expression_is_kept(self, expression):
    assert validate_backup_cron(expression) == expression

  @pytest.mark.parametrize("expression", ["* * * *", "* * * * * *", "@daily", "0"])
  def test_any_other_number_of_parts_is_a_400(self, expression):
    with pytest.raises(HTTPException) as raised:
      validate_backup_cron(expression)

    assert (raised.value.status_code, raised.value.detail) == (400, "backup_cron must be a 5-part cron expression.")

  @pytest.mark.parametrize("expression", ["61 * * * *", "0 25 * * *", "a b c d e", "0 0 32 * *"])
  def test_five_parts_that_are_not_a_schedule_are_a_400(self, expression):
    with pytest.raises(HTTPException) as raised:
      validate_backup_cron(expression)

    assert (raised.value.status_code, raised.value.detail) == (400, "Invalid backup_cron expression.")


class TestInjectVolume:
  def test_an_unknown_volume_is_a_404(self, client, application):
    response = client.put(f"{URL}/missing", json={"path": "/x"})
    assert (response.status_code, response.json()["detail"]) == (404, "Volume not found")

  def test_a_volume_of_another_application_is_a_404(self, client, application, volume):
    Application.create(project=application.project, name="api", label="Api")

    assert client.delete("/api/projects/demo/applications/api/volumes/data").status_code == 404
    assert Volume.select().count() == 1

  def test_an_unknown_application_is_a_404(self, client, project):
    assert client.get("/api/projects/demo/applications/missing/volumes/").status_code == 404


class TestList:
  def test_only_this_applications_volumes(self, client, application, volume):
    other = Application.create(project=application.project, name="api", label="Api")
    Volume.create(application=other, name="cache", path="/cache")

    assert [row["name"] for row in client.get(f"{URL}/").json()] == ["data"]


class TestCreate:
  def test_a_volume_is_created_for_the_application(self, client, application):
    response = client.post(f"{URL}/", json={"name": "data", "path": "/var/data", "backup_cron": "0 3 * * *"})

    assert response.status_code == 200
    volume = Volume.get()
    assert (volume.name, volume.path, volume.backup_cron, volume.application_id) == ("data", "/var/data", "0 3 * * *", application.id)

  @pytest.mark.parametrize("cron", [None, "", "   "])
  def test_no_schedule_is_stored_as_none(self, client, application, cron):
    client.post(f"{URL}/", json={"name": "data", "path": "/data", "backup_cron": cron})
    assert Volume.get().backup_cron is None

  def test_a_bad_schedule_is_a_400_and_nothing_is_created(self, client, application):
    response = client.post(f"{URL}/", json={"name": "data", "path": "/data", "backup_cron": "nonsense"})

    assert response.status_code == 400
    assert Volume.select().count() == 0

  @pytest.mark.parametrize("path", ["data", "/data:ro", "", None])
  def test_a_bad_path_is_a_400(self, client, application, path):
    response = client.post(f"{URL}/", json={"name": "data", "path": path})

    assert response.status_code == 400
    assert "Invalid volume path" in response.json()["detail"]

  def test_a_name_already_used_by_the_application_is_a_409(self, client, application, volume):
    response = client.post(f"{URL}/", json={"name": "data", "path": "/other"})
    assert response.status_code == 409

  def test_the_same_name_can_be_used_by_another_application(self, client, application, volume):
    Application.create(project=application.project, name="api", label="Api")
    assert client.post("/api/projects/demo/applications/api/volumes/", json={"name": "data", "path": "/data"}).status_code == 200

  def test_an_invalid_name_is_a_400(self, client, application):
    response = client.post(f"{URL}/", json={"name": "Not Valid", "path": "/data"})

    assert response.status_code == 400
    assert "Invalid name" in response.json()["detail"]


class TestUpdate:
  def test_name_path_and_schedule_are_changed(self, client, application, volume):
    response = client.put(f"{URL}/data", json={"name": "files", "path": " /srv/files ", "backup_cron": "0 4 * * *"})

    assert response.status_code == 200
    saved = Volume.get_by_id(volume.id)
    assert (saved.name, saved.path, saved.backup_cron) == ("files", "/srv/files", "0 4 * * *")

  def test_an_empty_schedule_clears_it(self, client, application, volume):
    volume.backup_cron = "0 3 * * *"
    volume.save()

    client.put(f"{URL}/data", json={"backup_cron": ""})

    assert Volume.get_by_id(volume.id).backup_cron is None

  def test_a_bad_schedule_is_a_400_and_the_old_one_stays(self, client, application, volume):
    volume.backup_cron = "0 3 * * *"
    volume.save()

    response = client.put(f"{URL}/data", json={"backup_cron": "nonsense"})

    assert response.status_code == 400
    assert Volume.get_by_id(volume.id).backup_cron == "0 3 * * *"

  def test_a_bad_path_is_a_400_and_the_old_one_stays(self, client, application, volume):
    response = client.put(f"{URL}/data", json={"path": "relative"})

    assert response.status_code == 400
    assert Volume.get_by_id(volume.id).path == "/data"

  def test_the_application_cannot_be_changed(self, client, application, volume):
    other = Application.create(project=application.project, name="api", label="Api")

    client.put(f"{URL}/data", json={"application": other.id, "path": "/same"})

    assert Volume.get_by_id(volume.id).application_id == application.id

  def test_renaming_to_a_name_the_application_already_uses_is_a_409(self, client, application, volume):
    Volume.create(application=application, name="logs", path="/logs")

    assert client.put(f"{URL}/logs", json={"name": "data"}).status_code == 409


class TestDelete:
  def test_the_volume_is_deleted(self, client, application, volume):
    assert client.delete(f"{URL}/data").json() == {"status": "deleted"}
    assert Volume.select().count() == 0
