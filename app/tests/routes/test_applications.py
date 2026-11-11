import pytest

import config
from services.db.models import Application, Container, Project, Worker
from services.logs import Logs

URL = "/api/projects/demo/applications"


def lines(application_name, rows):
  Logs().write_application_lines(application_name, [
      {"hostname": host, "ts": f"2026-01-01T12:00:0{index}.000000000Z", "stream": "stdout", "message": message}
      for index, (host, message) in enumerate(rows)])


class TestApplication:
  def test_a_new_application_is_named_from_its_label_and_answered_with_its_page(self, client, project, manager):
    manager.answers["application_page"] = lambda project, name: {"page": f"{project}/{name}"}
    response = client.post(f"{URL}/", json={"label": "My App", "description": "Notes", "image": "nginx"})
    assert response.json() == {"page": "demo/myapp"}
    application = Application.get(Application.name == "myapp")
    assert (application.label, application.description, application.image) == ("My App", "Notes", None)

  def test_a_new_one_in_an_unknown_project_is_a_404_and_one_of_a_name_taken_a_409(self, client, application):
    assert client.post("/api/projects/missing/applications/", json={"label": "Web"}).status_code == 404
    assert client.post(f"{URL}/", json={"label": "Web"}).status_code == 409

  def test_its_page_is_the_managers(self, client, manager):
    manager.answers["application_page"] = {"name": "web"}
    assert client.get(f"{URL}/web").json() == {"name": "web"}
    assert manager.called("application_page") == [(("demo", "web"), {})]

  def test_an_edit_is_written_and_answered_with_its_page(self, client, application, manager):
    manager.answers["application_page"] = {"name": "web"}
    assert client.put(f"{URL}/web", json={"image": "redis"}).json() == {"name": "web"}
    assert Application.get_by_id(application.id).image == "redis"

  def test_an_edit_of_an_unknown_application_or_one_of_another_project_is_a_404(self, client, application):
    other = Project.create(name="other", label="Other")
    assert client.put(f"{URL}/missing", json={"image": "redis"}).status_code == 404
    assert client.put("/api/projects/other/applications/web", json={"image": "redis"}).status_code == 404
    assert other.applications.count() == 0 and Application.get_by_id(application.id).image == "nginx"

  def test_an_application_without_containers_is_deleted_with_no_content(self, client, application):
    assert client.delete(f"{URL}/web").status_code == 204
    assert Application.select().count() == 0

  def test_one_with_containers_is_refused_and_stays(self, client, application, worker):
    Container.create(application=application, worker=worker)
    assert client.delete(f"{URL}/web").status_code == 409
    assert Application.select().count() == 1


class TestAllContainers:
  @pytest.mark.parametrize("action, request_name", [("deploy", "request_deploy_all"), ("stop", "request_stop_all")])
  def test_deploy_and_stop_ask_for_each_container_and_answer_the_tasks(self, client, application, manager, action,
                                                                       request_name):
    manager.answers[request_name] = ["t1", "t2"]
    assert client.post(f"{URL}/web/{action}").json() == {"task_ids": ["t1", "t2"]}
    assert manager.called(request_name) == [((application.id,), {})]

  def test_an_unknown_application_is_a_404_and_asks_for_nothing(self, client, project, manager):
    assert client.post(f"{URL}/missing/deploy").status_code == 404
    assert manager.calls == []


class TestMetricsAndLogs:
  def test_the_graphs_are_the_managers_for_the_period_asked(self, client, manager):
    manager.answers["application_graphs"] = []
    assert client.get(f"{URL}/web/metrics", params={"period": "24h"}).json() == []
    assert manager.called("application_graphs") == [(("demo", "web", "24h"), {})]

  def test_a_period_that_is_not_one_is_refused(self, client, manager):
    assert client.get(f"{URL}/web/metrics", params={"period": "2h"}).status_code == 422
    assert manager.calls == []

  def test_the_logs_are_the_applications_own_lines_filtered_as_asked(self, client, application):
    lines("demo-web", [("w1", "Started"), ("w2", "started again"), ("w2", "stopped")])
    lines("demo-other", [("w2", "started elsewhere")])
    response = client.get(f"{URL}/web/logs", params={"hostname": "w2", "search": "START", "limit": 5})
    assert ([row["message"] for row in response.json()["lines"]], response.json()["more"]) == (["started again"], False)

  @pytest.mark.parametrize("params", [{"from_ts": "yesterday"}, {"limit": 0}, {"limit": config.LIST_MAX_ROWS + 1}])
  def test_a_time_that_is_not_one_or_a_limit_out_of_bounds_is_refused(self, client, application, params):
    assert client.get(f"{URL}/web/logs", params=params).status_code == 422

  def test_an_unknown_application_has_no_logs(self, client, project):
    assert client.get(f"{URL}/missing/logs").status_code == 404


class TestContainers:
  def test_a_container_is_placed_on_a_worker_and_answered_with_its_payload(self, client, application, worker, manager):
    manager.answers["container_payloads"] = lambda containers: [{"id": container.id} for container in containers]
    response = client.post(f"{URL}/web/containers/", json={"worker": "w1"})
    container = Container.get(Container.application == application.id)
    assert (response.json(), container.worker_id) == ({"id": container.id}, "w1")

  def test_an_unknown_worker_is_a_404_and_a_second_on_one_worker_a_409(self, client, application, worker, manager):
    assert client.post(f"{URL}/web/containers/", json={"worker": "w9"}).status_code == 404
    manager.answers["container_payloads"] = lambda containers: [{}]
    client.post(f"{URL}/web/containers/", json={"worker": "w1"})
    assert client.post(f"{URL}/web/containers/", json={"worker": "w1"}).status_code == 409
    assert Container.select().count() == 1

  @pytest.mark.parametrize("path, request_name, arguments", [
      ("deploy", "request_deploy", ()), ("stop", "request_stop", ()), ("cancel/delete", "request_cancel", ("delete",))])
  def test_a_request_on_one_container_answers_its_task(self, client, application, worker, manager, path, request_name,
                                                       arguments):
    container = Container.create(application=application, worker=worker)
    manager.answers[request_name] = "t1"
    assert client.post(f"{URL}/web/containers/{container.id}/{path}").json() == {"task_id": "t1"}
    assert manager.called(request_name) == [((container.id, *arguments), {})]

  @pytest.mark.parametrize("query, force", [({}, False), ({"force": "true"}, True)])
  def test_a_delete_answers_its_task_and_force_reaches_it(self, client, application, worker, manager, query, force):
    container = Container.create(application=application, worker=worker)
    assert client.delete(f"{URL}/web/containers/{container.id}", params=query).json() == {"task_id": "task-1"}
    assert manager.called("request_delete") == [((container.id, force), {})]

  @pytest.mark.parametrize("method, path", [("post", "deploy"), ("post", "stop"), ("post", "cancel/deploy"), ("delete", "")])
  def test_a_container_of_another_application_or_none_is_a_404_and_asks_for_nothing(
          self, client, application, worker, manager, method, path):
    other = Application.create(project=application.project, name="api", label="Api")
    elsewhere = Container.create(application=other, worker=Worker.create(hostname="w2", subdomain="blue"))
    for container in (elsewhere.id, "c0000000000000000"):
      response = client.request(method.upper(), f"{URL}/web/containers/{container}/{path}".rstrip("/"))
      assert response.status_code == 404
    assert manager.calls == []
