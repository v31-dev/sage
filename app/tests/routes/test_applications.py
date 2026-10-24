import pytest

from services.db import Application, Container, Project, Volume, Worker

URL = "/api/projects/demo/applications"


class TestInjectApplication:
  def test_an_unknown_application_is_a_404(self, client, project):
    response = client.get(f"{URL}/missing")
    assert (response.status_code, response.json()["detail"]) == (404, "Application not found")

  def test_an_application_of_another_project_is_a_404(self, client, application):
    Project.create(name="other", label="Other")

    assert client.get("/api/projects/other/applications/web").status_code == 404

  def test_a_name_that_can_never_exist_is_a_400(self, client, project):
    assert client.get(f"{URL}/NotValid").status_code == 400


class TestCreate:
  def test_the_name_comes_from_the_label_and_the_project_from_the_url(self, client, project):
    response = client.post(f"{URL}/", json={"label": "My Web App", "description": " Public site "})

    assert response.status_code == 200
    created = Application.get(Application.name == "mywebapp")
    assert (created.label, created.description, created.project_id) == ("My Web App", "Public site", "demo")

  def test_a_new_application_starts_inactive_and_counts_toward_the_project(self, client, project):
    client.post(f"{URL}/", json={"label": "Web"})

    assert Application.get().status == "inactive"
    assert Project.get_by_id("demo").application_count == 1

  def test_a_name_already_used_in_the_project_is_a_409(self, client, application):
    response = client.post(f"{URL}/", json={"label": "WEB"})
    assert response.status_code == 409

  def test_the_same_name_can_be_used_in_another_project(self, client, application):
    Project.create(name="other", label="Other")
    assert client.post("/api/projects/other/applications/", json={"label": "Web"}).status_code == 200

  def test_a_label_that_gives_no_valid_name_is_a_400(self, client, project):
    assert client.post(f"{URL}/", json={"label": "!!!"}).status_code == 400

  @pytest.mark.parametrize("body", [{}, {"description": "Notes"}, {"label": None}, {"label": ""}])
  def test_a_missing_or_empty_label_is_a_400(self, client, project, body):
    response = client.post(f"{URL}/", json=body)

    assert response.status_code == 400
    assert "does not produce a valid name" in response.json()["detail"]
    assert Application.select().count() == 0

  def test_only_the_label_and_description_are_taken(self, client, project):
    client.post(f"{URL}/", json={"label": "Web", "status": "active", "image": "evil", "container_count": 3})

    created = Application.get()
    assert (created.status, created.image, created.container_count) == ("inactive", None, 0)


class TestGet:
  def test_it_returns_the_application_with_what_hangs_off_it(self, client, application, volume):
    body = client.get(f"{URL}/web").json()

    assert body["name"] == "web"
    assert body["project"]["name"] == "demo"
    assert [row["name"] for row in body["volumes"]] == ["data"]
    assert body["containers"] == []

  def test_a_new_application_is_not_dirty(self, client, application):
    assert client.get(f"{URL}/web").json()["config_dirty"] is False

  def test_changing_a_deploy_input_after_a_deploy_makes_it_dirty(self, client, application):
    application.deployed_hash = application.deploy_config.content_hash
    application.save()

    client.put(f"{URL}/web", json={"env": "A=1"})

    assert client.get(f"{URL}/web").json()["config_dirty"] is True


class TestUpdate:
  def test_the_listed_fields_are_changed_and_stripped(self, client, application):
    response = client.put(f"{URL}/web", json={
        "label": " Web 2 ", "description": "Notes", "image": " nginx:1.27 ", "env": "A=1", "command": " serve "})

    assert response.status_code == 200
    saved = Application.get_by_id(application.id)
    assert (saved.label, saved.description, saved.image, saved.env, saved.command) == ("Web 2", "Notes", "nginx:1.27", "A=1", "serve")

  @pytest.mark.parametrize("field, value", [
      ("name", "hacked"),
      ("status", "active"),
      ("project", "other"),
      ("container_count", 7),
      ("deployed_hash", "abc"),
      ("domains_synced", True),
      ("id", 99),
  ])
  def test_fields_the_user_does_not_own_are_ignored(self, client, application, field, value):
    Project.create(name="other", label="Other")
    before = Application.get_by_id(application.id)

    client.put(f"{URL}/web", json={"label": "Same", field: value})

    saved = Application.get_by_id(application.id)
    assert getattr(saved, field if field != "project" else "project_id") == getattr(before, field if field != "project" else "project_id")

  def test_switching_to_git_with_a_good_repository(self, client, application):
    response = client.put(f"{URL}/web", json={"type": "git", "repo": "https://github.com/user/repo.git", "path": "docker/Dockerfile"})

    assert response.status_code == 200
    saved = Application.get_by_id(application.id)
    assert (saved.type, saved.repo, saved.path, saved.image) == ("git", "https://github.com/user/repo.git", "docker/Dockerfile", None)

  @pytest.mark.parametrize("body, message", [
      ({"type": "git", "repo": "not a url"}, "Invalid Git repository URL"),
      ({"image": "bad image"}, "Invalid image reference"),
      ({"env": "9BAD=1"}, "application.env"),
  ])
  def test_a_value_the_model_refuses_is_a_400_and_nothing_is_stored(self, client, application, body, message):
    response = client.put(f"{URL}/web", json=body)

    assert response.status_code == 400
    assert message in response.json()["detail"]
    saved = Application.get_by_id(application.id)
    assert (saved.type, saved.image, saved.env) == ("docker", "nginx", None)


class TestDelete:
  def test_an_idle_application_without_containers_is_deleted(self, client, application):
    assert client.delete(f"{URL}/web").json() == {"status": "deleted"}
    assert Application.select().count() == 0
    assert Project.get_by_id("demo").application_count == 0

  @pytest.mark.parametrize("status", ["deploying", "stopping", "backup", "restoring"])
  def test_a_busy_application_is_refused(self, client, application, status):
    application.status = status
    application.save()

    response = client.delete(f"{URL}/web")

    assert response.status_code == 409
    assert response.json()["detail"] == f"Application is already {status}."
    assert Application.select().count() == 1

  @pytest.mark.parametrize("status", ["active", "inactive", "error"])
  def test_an_application_that_is_not_busy_can_be_deleted(self, client, application, status):
    application.status = status
    application.save()
    assert client.delete(f"{URL}/web").status_code == 200

  def test_an_application_with_containers_is_refused(self, client, application, worker):
    Container.create(application=application, worker=worker)

    response = client.delete(f"{URL}/web")

    assert response.status_code == 409
    assert "has containers" in response.json()["detail"]
    assert Application.select().count() == 1

  def test_its_volumes_go_with_it(self, client, application, volume):
    client.delete(f"{URL}/web")
    assert Volume.select().count() == 0


class TestAvailableWorkers:
  def test_every_worker_when_none_is_used(self, client, application, worker):
    Worker.create(hostname="w2", ip="100.64.0.3")

    response = client.post(f"{URL}/web/get_available_workers")

    assert sorted(row["hostname"] for row in response.json()) == ["w1", "w2"]

  def test_workers_already_running_the_application_are_left_out(self, client, application, worker):
    Worker.create(hostname="w2", ip="100.64.0.3")
    Container.create(application=application, worker=worker)

    response = client.post(f"{URL}/web/get_available_workers")

    assert [row["hostname"] for row in response.json()] == ["w2"]

  def test_a_worker_used_by_another_application_is_still_available(self, client, application, worker):
    other = Application.create(project=application.project, name="api", label="Api")
    Container.create(application=other, worker=worker)

    response = client.post(f"{URL}/web/get_available_workers")

    assert [row["hostname"] for row in response.json()] == ["w1"]

  def test_no_workers_at_all(self, client, application):
    assert client.post(f"{URL}/web/get_available_workers").json() == []
