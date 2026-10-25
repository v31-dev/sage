from datetime import datetime

import pytest

from services.reports import HostReading, Reports, Snapshot


def report(traefik_reachable):
  return [Snapshot(at=datetime(2026, 1, 1, 12), host=HostReading(*([None] * 13)), containers={},
                   traefik_reachable=traefik_reachable)]


class TestReady:
  def test_the_list_says_nothing_yet_of_a_worker_that_has_not_reported_since_the_manager_started(self, client, worker):
    (listed,) = client.get("/api/workers/").json()
    assert (listed["hostname"], listed["online"], listed["ready"]) == ("w1", False, None)

  def test_the_list_carries_it_for_every_worker(self, client, worker):
    Reports().accept("w1", report(True))
    (listed,) = client.get("/api/workers/").json()
    assert listed["ready"] is True

  def test_a_worker_whose_traefik_does_not_answer_is_not_ready(self, client, worker):
    Reports().accept("w1", report(False))
    assert client.get("/api/workers/w1").json()["ready"] is False

  def test_one_worker_carries_it_too(self, client, worker):
    Reports().accept("w1", report(True))
    detail = client.get("/api/workers/w1").json()
    assert (detail["hostname"], detail["ready"]) == ("w1", True)


class TestInfra:
  def test_the_detail_carries_the_state_of_the_workers_stack(self, client, worker):
    Reports().accept("w1", report(True))
    infra = client.get("/api/workers/w1").json()["infra"]
    assert [item["name"] for item in infra] == ["traefik", "vector", "telegraf", "cloudflared"]
    assert {item["status"] for item in infra} == {"missing"}

  def test_the_detail_has_none_for_a_worker_with_no_current_report(self, client, worker):
    assert client.get("/api/workers/w1").json()["infra"] is None


class TestState:
  def test_the_list_and_the_detail_carry_what_is_being_done_to_a_worker(self, client, worker, manager):
    assert client.get("/api/workers/w1").json()["state"] is None
    manager.worker_states["w1"] = "rebooting"
    assert client.get("/api/workers/").json()[0]["state"] == "rebooting"
    assert client.get("/api/workers/w1").json()["state"] == "rebooting"


class TestReboot:
  @pytest.fixture
  def online(self, worker):
    worker.online = True
    worker.save()
    return worker

  def test_it_queues_the_reboot_of_an_online_worker(self, client, online, manager):
    assert client.post("/api/workers/w1/reboot").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["reboot_worker"]

  @pytest.mark.parametrize("state", ["rebooting", "removing"])
  def test_a_worker_that_is_already_being_rebooted_or_removed_is_refused(self, client, online, manager, state):
    manager.worker_states["w1"] = state
    response = client.post("/api/workers/w1/reboot")
    assert (response.status_code, response.json()["detail"]) == (409, f"Worker is {state}.")
    assert manager.tasks == []

  def test_an_offline_worker_is_refused(self, client, worker, manager):
    assert client.post("/api/workers/w1/reboot").status_code == 409
    assert manager.tasks == []


class TestDelete:
  def test_it_queues_the_removal(self, client, worker, manager):
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_a_worker_already_being_removed_is_refused(self, client, worker, manager):
    manager.worker_states["w1"] = "removing"
    assert client.delete("/api/workers/w1").status_code == 409
    assert manager.tasks == []

  def test_a_worker_that_is_rebooting_can_still_be_removed(self, client, worker, manager):
    manager.worker_states["w1"] = "rebooting"
    assert client.delete("/api/workers/w1").status_code == 200
