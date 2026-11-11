from fastapi.encoders import jsonable_encoder

from services.applications.payload import project_payload
from services.db.models import Project

URL = "/api/projects"


class TestList:
  def test_every_project_by_name(self, client, db):
    Project.create(name="web", label="Web")
    Project.create(name="api", label="Api")
    assert [row["name"] for row in client.get(f"{URL}/").json()] == ["api", "web"]


class TestCreate:
  def test_a_project_is_named_from_its_label_and_answered_with_its_payload(self, client, db):
    response = client.post(f"{URL}/", json={"label": "My Project", "description": "Notes", "env": "A=1"})
    assert response.status_code == 200
    assert response.json() == jsonable_encoder(project_payload(Project.get_by_id("myproject")))
    assert (response.json()["description"], response.json()["env"]) == ("Notes", None)

  def test_a_name_already_taken_is_a_409_and_a_label_that_gives_none_a_400(self, client, project):
    assert client.post(f"{URL}/", json={"label": "Demo!"}).status_code == 409
    assert client.post(f"{URL}/", json={"label": "123"}).status_code == 400
    assert Project.select().count() == 1


class TestPage:
  def test_it_is_the_managers_page_of_the_project(self, client, manager):
    manager.answers["project_page"] = {"name": "demo", "applications": []}
    assert client.get(f"{URL}/demo").json() == {"name": "demo", "applications": []}
    assert manager.called("project_page") == [(("demo",), {})]


class TestEdit:
  def test_an_edit_is_written_and_answered_with_the_payload(self, client, project):
    response = client.put(f"{URL}/demo", json={"label": "Renamed"})
    assert (response.status_code, response.json()["name"], response.json()["label"]) == (200, "demo", "Renamed")
    assert Project.get_by_id("demo").label == "Renamed"

  def test_a_field_the_project_does_not_own_is_refused_and_nothing_written(self, client, project):
    response = client.put(f"{URL}/demo", json={"label": "Renamed", "name": "other"})
    assert (response.status_code, response.json()) == (400, {"detail": "Unknown fields: name."})
    assert Project.get_by_id("demo").label == "Demo"

  def test_an_unknown_project_is_a_404_and_a_name_none_can_have_a_400(self, client, db):
    assert client.put(f"{URL}/missing", json={"label": "X"}).status_code == 404
    assert client.put(f"{URL}/NotValid", json={"label": "X"}).status_code == 400


class TestDelete:
  def test_an_empty_project_is_deleted_with_no_content(self, client, project):
    response = client.delete(f"{URL}/demo")
    assert (response.status_code, response.content) == (204, b"")
    assert Project.select().count() == 0

  def test_a_project_with_applications_is_refused_and_stays(self, client, application):
    assert client.delete(f"{URL}/demo").status_code == 409
    assert Project.select().count() == 1
