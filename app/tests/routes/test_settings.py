import pytest

from services.db.models import Worker
from services.settings import Settings

URL = "/api/settings"
STORE = {"access_key": "a", "secret_key": "s", "bucket": "b", "endpoint": "https://s3.example.test", "path": "sage"}


@pytest.fixture
def settings(db):
  return Settings()


class TestRead:
  def test_a_section_is_its_saved_row(self, client, settings):
    settings.set("s3", STORE)
    assert client.get(f"{URL}/s3").json()["value"] == STORE

  @pytest.mark.parametrize("section", ["cloudflare", "tailscale", "missing"])
  def test_a_section_not_saved_in_the_table_is_a_404(self, client, settings, section):
    assert client.get(f"{URL}/{section}").status_code == 404


class TestSave:
  def test_the_fields_sent_are_saved_over_the_others_then_the_save_handed_on_and_the_row_answered(
          self, client, settings, manager):
    settings.set("s3", STORE)
    response = client.put(f"{URL}/s3", json={"value": {"bucket": "other"}})
    assert response.json()["value"] == {**STORE, "bucket": "other"} == settings.get("s3")
    assert manager.called("settings_saved") == [(("s3",), {})]

  @pytest.mark.parametrize("section, value, status", [
      ("s3", {"bucket": "b"}, 400), ("s3", {**STORE, "region": "eu"}, 400), ("s3", {"bucket": 5}, 400),
      ("s3", None, 400), ("s3", "bucket", 400), ("cloudflare", {"zone": "z"}, 404)])
  def test_values_the_section_refuses_are_not_saved_and_hand_nothing_on(self, client, settings, manager, section, value,
                                                                        status):
    assert client.put(f"{URL}/{section}", json={"value": value}).status_code == status
    assert settings.get("s3") == dict.fromkeys(STORE)
    assert manager.calls == []


class TestTest:
  def test_the_saved_values_with_those_sent_are_tested_and_its_message_answered(self, client, settings, manager):
    settings.set("s3", STORE)
    manager.answers["test_setting"] = lambda section, values: f"{section} {values['bucket']}"
    assert client.post(f"{URL}/s3/test", json={"value": {"bucket": "other"}}).json() == {"message": "s3 other"}
    assert manager.called("test_setting") == [(("s3", {**STORE, "bucket": "other"}), {})]
    assert settings.get("s3") == STORE

  def test_a_failed_test_is_a_400_with_its_described_reason(self, client, settings, manager):
    manager.answers["test_setting"] = TimeoutError("timed out")
    manager.answers["describe_check_error"] = lambda error: (f"described: {error}", True)
    response = client.post(f"{URL}/notifications/test", json={"value": {}})
    assert (response.status_code, response.json()["detail"]) == (400, "described: timed out")


class TestQueue:
  @pytest.mark.parametrize("path, request_name", [("pause", "pause_tasks"), ("resume", "resume_tasks")])
  def test_pause_and_resume_answer_the_state_they_leave(self, client, manager, path, request_name):
    manager.state = "paused"
    assert client.post(f"{URL}/{path}").json() == {"state": "paused"}
    assert manager.called(request_name) == [((), {})]

  def test_resync_workers_asks_for_each_workers_resync(self, client, manager, db):
    for hostname in ("w2", "w1"):
      Worker.create(hostname=hostname, subdomain=f"s{hostname}")
    manager.answers["request_convergence"] = lambda hostname, force: f"task-{hostname}"
    assert client.post(f"{URL}/resync_workers").json() == {"task_ids": ["task-w1", "task-w2"]}
    assert manager.called("request_convergence") == [(("w1",), {"force": True}), (("w2",), {"force": True})]
