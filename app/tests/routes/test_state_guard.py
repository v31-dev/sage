import pytest
from fastapi.testclient import TestClient

from utils.errors import Conflict

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
  @pytest.mark.parametrize("method, url", [("post", "/api/release/refresh"), ("put", "/api/anything"),
                                           ("patch", "/api/anything"), ("delete", "/api/anything")])
  def test_a_change_is_a_503_that_says_why_and_asks_for_nothing(self, client, manager, state, message, method, url):
    manager.state = state
    response = client.request(method.upper(), url, json={})
    assert (response.status_code, response.json()) == (503, {"detail": message})
    assert manager.calls == []

  @pytest.mark.parametrize("method", ["get", "head", "options"])
  def test_reading_still_works(self, client, manager, state, message, method):
    manager.state = state
    assert getattr(client, method)("/api/tasks/").status_code != 503

  @pytest.mark.parametrize("url", ["/api/settings/pause", "/api/settings/resume"])
  def test_pause_and_resume_are_never_blocked(self, lenient, manager, state, message, url):
    manager.state = state
    assert lenient.post(url).status_code != 503


class TestStateChangedWhileTheChangeRan:
  def becomes(self, manager, state, answer="task-1"):
    return lambda *args, **kwargs: setattr(manager, "state", state) or answer

  def test_a_restore_started_meanwhile_makes_it_the_restoring_503(self, client, manager):
    manager.answers["request_release_check"] = self.becomes(manager, "restoring", answer=None)
    response = client.post("/api/release/refresh")
    assert (response.status_code, response.json()) == (503, {"detail": MESSAGES["restoring"]})

  def test_the_restore_request_itself_answers_its_task(self, client, manager):
    manager.answers["request_platform_restore"] = self.becomes(manager, "restoring", answer="r1")
    response = client.post("/api/backups/7/restore")
    assert (response.status_code, response.json()) == (200, {"task_id": "r1"})

  @pytest.mark.parametrize("state", ["paused", "stopping"])
  def test_a_pause_or_stop_meanwhile_leaves_its_answer(self, client, manager, state):
    manager.answers["request_release_check"] = self.becomes(manager, state)
    response = client.post("/api/release/refresh")
    assert (response.status_code, response.json()) == (200, {"task_id": "task-1"})


class TestRunning:
  def test_a_change_goes_through_and_answers_its_task(self, client, manager):
    response = client.post("/api/release/refresh")
    assert (response.status_code, response.json()) == (200, {"task_id": "task-1"})
    assert manager.called("request_release_check") == [((), {"quiet": False})]

  @pytest.mark.parametrize("error, status", [(ValueError("No such period."), 400), (LookupError("Worker not found."), 404),
                                             (Conflict("This container already has an operation in progress."), 409)])
  def test_what_a_request_refuses_is_its_status_with_its_message(self, client, manager, error, status):
    manager.answers["request_release_check"] = error
    response = client.post("/api/release/refresh")
    assert (response.status_code, response.json()) == (status, {"detail": str(error)})

  @pytest.mark.parametrize("error", [KeyError("hostname"), IndexError("list index out of range"), RuntimeError("boom")])
  def test_any_other_error_is_a_500(self, lenient, manager, error):
    manager.answers["request_release_check"] = error
    assert lenient.post("/api/release/refresh").status_code == 500
