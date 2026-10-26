from datetime import datetime

import pytest

from services.db import Container
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


class TestPath:
  def test_the_list_and_the_detail_say_how_the_worker_is_reached(self, client, worker, tailscale):
    tailscale.paths["100.64.0.2"] = "relay blr"
    assert client.get("/api/workers/").json()[0]["path"] == "relay blr"
    assert client.get("/api/workers/w1").json()["path"] == "relay blr"

  def test_a_worker_that_has_not_answered_has_none(self, client, worker, tailscale):
    assert client.get("/api/workers/").json()[0]["path"] is None
    assert client.get("/api/workers/w1").json()["path"] is None


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


class TestAdd:
  @pytest.fixture(autouse=True)
  def machines(self, tailscale):
    tailscale.machines = {"w1": "100.64.0.2", "w9": "100.64.0.9"}

  def test_a_machine_of_the_tailnet_is_queued_to_be_added(self, client, manager):
    response = client.post("/api/workers/", json={"host": "w9"})
    assert (response.status_code, response.json()["hostname"]) == (200, "w9")
    (task,) = manager.tasks
    assert (task["task"].__name__, task["params"]) == ("add_worker", {"worker_hostname": "w9"})

  def test_it_can_be_given_by_its_address_in_any_case_and_the_hostname_is_what_is_added(self, client, manager):
    assert client.post("/api/workers/", json={"host": " 100.64.0.9 "}).status_code == 200
    assert client.post("/api/workers/", json={"host": "W9"}).status_code == 200
    assert [task["params"]["worker_hostname"] for task in manager.tasks] == ["w9", "w9"]

  @pytest.mark.parametrize("body", [{"host": "nobody"}, {"host": ""}, {"host": None}, {}])
  def test_a_machine_that_is_not_in_the_tailnet_is_refused(self, client, manager, body):
    assert client.post("/api/workers/", json=body).status_code == 404
    assert manager.tasks == []

  def test_a_worker_that_is_already_added_is_refused(self, client, worker, manager):
    response = client.post("/api/workers/", json={"host": "w1"})
    assert (response.status_code, response.json()["detail"]) == (409, "Worker w1 is already added.")
    assert manager.tasks == []

  def test_a_machine_that_is_already_being_added_is_refused(self, client, manager):
    manager.queued = {"add_worker"}
    response = client.post("/api/workers/", json={"host": "w9"})
    assert (response.status_code, response.json()["detail"]) == (409, "Worker w9 is already being added.")
    assert manager.tasks == []


class TestDelete:
  def test_it_queues_the_removal(self, client, worker, manager):
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_an_online_worker_with_containers_is_refused_until_they_are_deleted(self, client, worker, application, manager):
    worker.online = True
    worker.save()
    Container.create(application=application, worker=worker)
    response = client.delete("/api/workers/w1")
    assert (response.status_code, response.json()["detail"]) == (400, "Worker is online and has containers; delete them first.")
    assert manager.tasks == []

  def test_an_offline_worker_with_containers_is_removed_with_them(self, client, worker, application, manager):
    Container.create(application=application, worker=worker)
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_an_online_worker_without_containers_is_removed(self, client, worker, manager):
    worker.online = True
    worker.save()
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_a_worker_already_being_removed_is_refused(self, client, worker, manager):
    manager.worker_statuses["w1"] = "removing"
    assert client.delete("/api/workers/w1").status_code == 409
    assert manager.tasks == []

  def test_a_worker_that_is_rebooting_can_still_be_removed(self, client, worker, manager):
    manager.worker_statuses["w1"] = "rebooting"
    assert client.delete("/api/workers/w1").status_code == 200
