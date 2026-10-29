from datetime import datetime

import pytest

from services.db import Container
from services.worker_reports import HostReading, Snapshot, WorkerReports


def report():
  return [Snapshot(at=datetime(2026, 1, 1, 12), host=HostReading(*([None] * 13)), containers={}, traefik_reachable=True)]


def listed(hostname="w1", **overrides):
  return {"hostname": hostname, "ip": "100.64.0.2", "online": True, "status": "online", "path": None, "container_count": None,
          "infra": None, "metrics": None, **overrides}


class TestInfra:
  def test_the_detail_carries_the_state_of_the_workers_stack_that_the_list_holds(self, client, worker, manager):
    stack = [{"name": "traefik", "status": "running"}, {"name": "vector", "status": "missing"}]
    manager.worker_list = [listed(infra=stack)]
    assert client.get("/api/workers/w1").json()["infra"] == stack

  def test_the_detail_has_none_for_a_worker_with_no_current_report(self, client, worker, manager):
    manager.worker_list = [listed()]
    assert client.get("/api/workers/w1").json()["infra"] is None


class TestList:
  def test_the_list_is_what_the_manager_holds_of_the_workers(self, client, manager):
    manager.worker_list = [
        listed("w1", status="rebooting", path="direct", container_count=3),
        listed("w2", ip="100.64.0.3", online=False, status="offline"),
    ]
    assert client.get("/api/workers/").json() == manager.worker_list

  def test_there_are_no_workers_before_any_is_added(self, client, manager):
    assert client.get("/api/workers/").json() == []


class TestEntry:
  def test_the_detail_carries_the_status_online_and_path_the_list_holds(self, client, worker, manager):
    manager.worker_list = [listed(status="rebooting", online=True, path="relay blr")]
    detail = client.get("/api/workers/w1").json()
    assert (detail["status"], detail["online"], detail["path"]) == ("rebooting", True, "relay blr")

  def test_a_worker_the_list_does_not_hold_is_offline_with_nothing_known(self, client, worker, manager):
    detail = client.get("/api/workers/w1").json()
    assert (detail["status"], detail["online"], detail["path"], detail["infra"]) == ("offline", False, None, None)
    assert detail["metrics"] is None

  def test_the_detail_and_the_list_carry_the_newest_minute_the_list_holds(self, client, worker, manager):
    minute = {"ts": "2026-01-01T12:00:00+00:00", "cpu_pct": 12.5, "cpu_cores": 4}
    manager.worker_list = [listed(metrics=minute)]
    assert client.get("/api/workers/w1").json()["metrics"] == minute
    assert client.get("/api/workers/").json()[0]["metrics"] == minute

  def test_it_is_the_workers_row_and_its_containers_besides(self, client, worker, manager, application):
    Container.create(application=application, worker=worker)
    manager.worker_list = [listed()]
    detail = client.get("/api/workers/w1").json()
    assert detail["hostname"] == "w1" and detail["ip"] == "100.64.0.2"
    assert [container["application"]["name"] for container in detail["containers"]] == ["web"]

  def test_a_worker_that_is_not_in_the_table_is_not_found(self, client, manager):
    manager.worker_list = [listed("w9")]
    assert client.get("/api/workers/w9").status_code == 404


class TestReboot:
  @pytest.fixture
  def online(self, worker, manager):
    manager.online.add(worker.hostname)
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
    manager.online.add("w1")
    Container.create(application=application, worker=worker)
    response = client.delete("/api/workers/w1")
    assert (response.status_code, response.json()["detail"]) == (400, "Worker is online and has containers; delete them first.")
    assert manager.tasks == []

  def test_a_worker_no_sync_has_looked_at_with_containers_is_refused_too(self, client, worker, application, manager):
    manager.checking.add("w1")
    Container.create(application=application, worker=worker)
    assert client.delete("/api/workers/w1").status_code == 400
    assert manager.tasks == []

  def test_an_offline_worker_with_containers_is_removed_with_them(self, client, worker, application, manager):
    Container.create(application=application, worker=worker)
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_an_online_worker_without_containers_is_removed(self, client, worker, manager):
    manager.online.add("w1")
    assert client.delete("/api/workers/w1").status_code == 200
    assert [task["task"].__name__ for task in manager.tasks] == ["remove_worker"]

  def test_a_worker_already_being_removed_is_refused(self, client, worker, manager):
    manager.worker_statuses["w1"] = "removing"
    assert client.delete("/api/workers/w1").status_code == 409
    assert manager.tasks == []

  def test_a_worker_that_is_rebooting_can_still_be_removed(self, client, worker, manager):
    manager.worker_statuses["w1"] = "rebooting"
    assert client.delete("/api/workers/w1").status_code == 200
