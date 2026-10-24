import pytest

from services.db import Application, Project


class TestInjectProject:
  def test_an_unknown_project_is_a_404(self, client):
    response = client.get("/api/projects/missing")
    assert (response.status_code, response.json()["detail"]) == (404, "Project not found")

  def test_a_name_that_can_never_exist_is_a_400(self, client):
    response = client.get("/api/projects/NotValid")

    assert response.status_code == 400
    assert "Invalid name" in response.json()["detail"]

  def test_every_nested_route_needs_the_project_to_exist(self, client):
    assert client.get("/api/projects/missing/applications/").status_code == 404
    assert client.post("/api/projects/missing/applications/", json={"label": "Web"}).status_code == 404


class TestList:
  def test_no_projects(self, client):
    assert client.get("/api/projects/").json() == []

  def test_every_project(self, client, project):
    Project.create(name="other", label="Other")
    assert [row["name"] for row in client.get("/api/projects/").json()] == ["demo", "other"]


class TestCreate:
  def test_the_name_is_derived_from_the_label(self, client):
    response = client.post("/api/projects/", json={"label": "  My Project 2  ", "description": "  Notes "})

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["label"], body["description"]) == ("myproject2", "My Project 2", "Notes")
    assert Project.get_by_id("myproject2").label == "My Project 2"

  def test_a_label_that_gives_no_valid_name_is_a_400(self, client):
    response = client.post("/api/projects/", json={"label": "123"})

    assert response.status_code == 400
    assert "does not produce a valid name" in response.json()["detail"]
    assert Project.select().count() == 0

  @pytest.mark.parametrize("body", [{}, {"description": "Notes"}, {"label": None}, {"label": ""}])
  def test_a_missing_or_empty_label_is_a_400(self, client, body):
    response = client.post("/api/projects/", json=body)

    assert response.status_code == 400
    assert "does not produce a valid name" in response.json()["detail"]

  def test_a_label_that_gives_a_name_already_taken_is_a_409(self, client, project):
    response = client.post("/api/projects/", json={"label": "DEMO!"})

    assert response.status_code == 409
    assert response.json()["detail"].startswith("Conflict:")

  def test_only_the_label_and_description_are_taken(self, client):
    client.post("/api/projects/", json={"label": "Web", "env": "A=1", "application_count": 5, "name": "hacked"})

    project = Project.get_by_id("web")
    assert (project.env, project.application_count) == (None, 0)


class TestGet:
  def test_the_project_with_its_applications(self, client, application):
    body = client.get("/api/projects/demo").json()

    assert body["label"] == "Demo"
    assert [row["name"] for row in body["applications"]] == ["web"]


class TestUpdate:
  def test_label_description_and_env_are_changed_and_stripped(self, client, project):
    response = client.put("/api/projects/demo", json={"label": " Renamed ", "description": "Notes", "env": " A=1 "})

    assert response.status_code == 200
    saved = Project.get_by_id("demo")
    assert (saved.label, saved.description, saved.env) == ("Renamed", "Notes", "A=1")

  def test_the_name_and_the_counter_cannot_be_changed(self, client, project):
    client.put("/api/projects/demo", json={"name": "other", "application_count": 9, "label": "Same"})

    saved = Project.get_by_id("demo")
    assert (saved.label, saved.application_count) == ("Same", 0)
    assert Project.select().count() == 1

  def test_an_invalid_env_is_a_400_and_nothing_is_stored(self, client, project):
    response = client.put("/api/projects/demo", json={"env": "9BAD=1"})

    assert response.status_code == 400
    assert "project.env" in response.json()["detail"]
    assert Project.get_by_id("demo").env is None


class TestDelete:
  def test_an_empty_project_is_deleted(self, client, project):
    assert client.delete("/api/projects/demo").json() == {"status": "deleted"}
    assert Project.select().count() == 0

  def test_a_project_with_applications_is_refused_and_stays(self, client, application):
    response = client.delete("/api/projects/demo")

    assert response.status_code == 409
    assert "has applications" in response.json()["detail"]
    assert Project.select().count() == 1

  def test_it_can_be_deleted_once_its_applications_are_gone(self, client, application):
    assert client.delete("/api/projects/demo/applications/web").status_code == 200
    assert client.delete("/api/projects/demo").status_code == 200


class TestApplicationsOfAProject:
  def test_only_this_projects_applications_are_listed(self, client, application):
    other = Project.create(name="other", label="Other")
    Application.create(project=other, name="api", label="Api")

    assert [row["name"] for row in client.get("/api/projects/demo/applications/").json()] == ["web"]
