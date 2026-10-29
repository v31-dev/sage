import pytest
from fastapi.testclient import TestClient

from services.db import Project

MESSAGES = {
    "paused": "Sage is paused; resume it first.",
    "restoring": "Sage is restoring a backup; wait for it to finish.",
    "stopping": "Sage is stopping.",
}


@pytest.fixture
def lenient(manager, thread_connections):
  """A client that returns a 500 for a handler that fails, instead of raising it."""
  from api import app
  return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("state, message", MESSAGES.items())
class TestChangesRefused:
  @pytest.mark.parametrize("method, url", [
      ("post", "/api/projects/"),
      ("put", "/api/projects/demo"),
      ("delete", "/api/projects/demo"),
      ("post", "/api/backups/"),
      ("post", "/api/projects/demo/applications/"),
      ("post", "/api/settings/s3/test"),
  ])
  def test_a_change_is_a_503_that_says_why(self, client, manager, project, state, message, method, url):
    manager.state = state

    response = client.request(method.upper(), url, json={"label": "x"})

    assert response.status_code == 503
    assert response.json() == {"detail": message}

  def test_nothing_is_changed(self, client, manager, project, state, message):
    manager.state = state

    client.delete("/api/projects/demo")
    client.put("/api/projects/demo", json={"label": "Changed"})

    assert Project.get_by_id("demo").label == "Demo"

  @pytest.mark.parametrize("method", ["get", "head", "options"])
  def test_reading_still_works(self, client, manager, project, state, message, method):
    manager.state = state
    assert getattr(client, method)("/api/projects/").status_code != 503

  def test_listing_returns_the_data(self, client, manager, project, state, message):
    manager.state = state
    assert [row["name"] for row in client.get("/api/projects/").json()] == ["demo"]

  @pytest.mark.parametrize("url", ["/api/settings/pause", "/api/settings/resume"])
  def test_pause_and_resume_are_never_blocked(self, lenient, manager, state, message, url):
    manager.state = state
    assert lenient.post(url).status_code != 503


class TestRunning:
  def test_changes_go_through(self, client, manager):
    assert client.post("/api/projects/", json={"label": "Web"}).status_code == 200

  def test_a_value_error_from_a_route_is_a_400_with_its_message(self, client, manager):
    response = client.post("/api/projects/", json={"label": "123"})

    assert response.status_code == 400
    assert "does not produce a valid name" in response.json()["detail"]
