from datetime import datetime

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
