from datetime import datetime

import pytest

from services.reports import HostReading, Reports, Snapshot


def report():
  return [Snapshot(at=datetime(2026, 1, 1, 12), host=HostReading(*([None] * 13)), containers={}, traefik_reachable=True)]


class TestInfra:
  def test_the_detail_carries_the_state_of_the_workers_stack(self, client, worker):
    Reports().accept("w1", report())
    infra = client.get("/api/workers/w1").json()["infra"]
    assert [item["name"] for item in infra] == ["traefik", "vector", "telegraf", "cloudflared"]
    assert {item["status"] for item in infra} == {"missing"}

  def test_the_detail_has_none_for_a_worker_with_no_current_report(self, client, worker):
    assert client.get("/api/workers/w1").json()["infra"] is None


class TestStatus:
  def test_the_list_and_the_detail_carry_the_status_the_manager_gives(self, client, worker, manager):
    manager.worker_statuses["w1"] = "rebooting"
    assert client.get("/api/workers/").json()[0]["status"] == "rebooting"
    assert client.get("/api/workers/w1").json()["status"] == "rebooting"

  def test_the_manager_is_told_whether_the_worker_answers(self, client, worker):
    assert client.get("/api/workers/").json()[0]["status"] == "offline"
    assert client.get("/api/workers/w1").json()["status"] == "offline"
    worker.online = True
    worker.save()
    assert client.get("/api/workers/").json()[0]["status"] == "online"
    assert client.get("/api/workers/w1").json()["status"] == "online"


class TestReboot:
  @pytest.fixture
  def online(self, worker):
    worker.online = True
    worker.save()
    return worker

  def test_it_queues_the_reboot_of_an_online_worker(self, client, online, manager):
    assert client.post("/api/workers/w1/reboot").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["reboot_worker"]

  @pytest.mark.parametrize("status", ["rebooting", "removing", "offline"])
  def test_a_worker_that_is_being_rebooted_or_removed_or_does_not_answer_is_refused(self, client, online, manager, status):
    manager.worker_statuses["w1"] = status
    response = client.post("/api/workers/w1/reboot")
    assert (response.status_code, response.json()["detail"]) == (409, f"Worker is {status}.")
    assert manager.tasks == []

  def test_a_worker_that_is_not_ready_can_be_rebooted(self, client, online, manager):
    manager.worker_statuses["w1"] = "not ready"
    assert client.post("/api/workers/w1/reboot").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["reboot_worker"]

  def test_an_offline_worker_is_refused(self, client, worker, manager):
    assert client.post("/api/workers/w1/reboot").status_code == 409
    assert manager.tasks == []


class TestDelete:
  def test_it_queues_the_removal(self, client, worker, manager):
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_a_worker_already_being_removed_is_refused(self, client, worker, manager):
    manager.worker_statuses["w1"] = "removing"
    assert client.delete("/api/workers/w1").status_code == 409
    assert manager.tasks == []

  def test_a_worker_that_is_rebooting_can_still_be_removed(self, client, worker, manager):
    manager.worker_statuses["w1"] = "rebooting"
    assert client.delete("/api/workers/w1").status_code == 200
