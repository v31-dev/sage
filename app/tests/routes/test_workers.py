import pytest

from services.db.models import Worker

URL = "/api/workers"


class TestReads:
  def test_the_list_is_the_managers(self, client, manager):
    assert client.get(f"{URL}/").status_code == 200
    assert manager.called("worker_list") == [((), {})]

  def test_a_page_is_the_managers_for_its_hostname(self, client, manager):
    manager.answers["worker_page"] = lambda hostname: {"hostname": hostname}
    assert client.get(f"{URL}/w1").json() == {"hostname": "w1"}

  def test_the_graphs_are_the_managers_for_the_period_asked(self, client, manager):
    manager.answers["worker_graphs"] = {}
    client.get(f"{URL}/w1/metrics", params={"period": "24h"})
    client.get(f"{URL}/manager/metrics")
    assert manager.called("worker_graphs") == [(("w1", "24h"), {}), (("manager", "1h"), {})]

  def test_a_period_that_is_not_one_is_refused(self, client, manager):
    assert client.get(f"{URL}/w1/metrics", params={"period": "2h"}).status_code == 422
    assert manager.calls == []


class TestAdd:
  def test_the_machine_asked_for_is_added_and_its_hostname_and_task_answered(self, client, manager):
    manager.answers["request_add_worker"] = ("w7", "t1")
    response = client.post(f"{URL}/", json={"host": "100.64.0.7", "subdomain": "red"})
    assert response.json() == {"task_id": "t1", "hostname": "w7"}
    assert manager.called("request_add_worker") == [(("100.64.0.7", "red"), {})]


class TestEdit:
  @pytest.fixture
  def page(self, manager):
    manager.answers["worker_page"] = lambda hostname: {"hostname": hostname}

  def test_a_new_subdomain_is_written_its_follow_ups_asked_and_the_page_answered(self, client, worker, manager, page):
    assert client.put(f"{URL}/w1", json={"subdomain": " blue "}).json() == {"hostname": "w1"}
    assert Worker.get_by_id("w1").subdomain == "blue"
    assert manager.called("worker_subdomain_changed") == [(("w1",), {})]

  def test_the_same_subdomain_asks_for_nothing(self, client, worker, manager, page):
    assert client.put(f"{URL}/w1", json={"subdomain": "red"}).status_code == 200
    assert manager.called("worker_subdomain_changed") == []

  @pytest.mark.parametrize("hostname, body, status", [
      ("w9", {"subdomain": "blue"}, 404), ("manager", {"subdomain": "blue"}, 404), ("w1", {"subdomain": "green"}, 409),
      ("w1", {}, 400), ("w1", {"subdomain": "Blue"}, 400)])
  def test_what_the_command_refuses_is_its_status_and_asks_for_nothing(self, client, worker, manager, page, hostname,
                                                                       body, status):
    Worker.create(hostname="w2", subdomain="green")
    assert client.put(f"{URL}/{hostname}", json=body).status_code == status
    assert Worker.get_by_id("w1").subdomain == "red"
    assert manager.calls == []


class TestTasks:
  @pytest.mark.parametrize("method, path, request_name, arguments", [
      ("DELETE", "", "request_remove_worker", (("w1",), {})),
      ("POST", "/reboot", "request_reboot", (("w1",), {})),
      ("POST", "/resync", "request_convergence", (("w1",), {"force": True}))])
  def test_each_answers_the_task_it_asked_for(self, client, worker, manager, method, path, request_name, arguments):
    manager.answers[request_name] = "t1"
    assert client.request(method, f"{URL}/w1{path}").json() == {"task_id": "t1"}
    assert manager.called(request_name) == [arguments]

  def test_a_resync_of_a_worker_not_configured_is_a_404_and_asks_for_nothing(self, client, worker, manager):
    for hostname in ("w9", "manager"):
      assert client.post(f"{URL}/{hostname}/resync").status_code == 404
    assert manager.calls == []
