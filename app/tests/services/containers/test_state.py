from datetime import datetime

from services.containers.decisions import ContainerView, Runtime, RuntimeReading
from services.containers.record import Applied, Authority, Settled, TargetRead, TargetRecord
from services.containers.state import ContainerState, container_payload
from services.db.models import Container, ContainerEvent

AT = datetime(2026, 1, 1, 9)


def state(container, headline="active", live=None):
  authority = Authority("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", container.id)
  record = TargetRecord(authority, Applied("d1", "h1", AT), Settled("d1", "running", "succeeded", AT))
  return ContainerState(ContainerView(headline, "current"), TargetRead("present", record),
                        Runtime("known", RuntimeReading("running", "d1", health="healthy")), live, True)


def test_a_container_is_its_placement_and_its_decided_state_with_what_it_was_decided_from(application, worker):
  container = Container.create(application=application, worker=worker)
  payload = container_payload(container, state(container, "deploying", live="deploy"), [{"id": "domain"}], [])
  assert (payload["id"], payload["worker"], payload["application"]) == (
      container.id, {"hostname": "w1", "subdomain": "red"},
      {"name": "web", "label": "Web", "project": {"name": "demo", "label": "Demo"}})
  assert payload["view"] == {"headline": "deploying", "config": "current", "reason": None, "detail": None}
  assert (payload["live"], payload["routable"], payload["domains"]) == ("deploy", True, [{"id": "domain"}])
  assert payload["target"]["kind"] == "present"
  assert payload["target"]["record"]["applied"] == {"deployment_id": "d1", "config_hash": "h1", "committed_at": AT}
  assert payload["runtime"]["knowledge"] == "known"
  assert (payload["runtime"]["reading"]["state"], payload["runtime"]["reading"]["health"]) == ("running", "healthy")


def test_its_events_are_given_in_the_order_they_come_with_what_tells_them_apart(application, worker):
  container = Container.create(application=application, worker=worker)
  events = [ContainerEvent.create(container=container, subject_id=container.id, action=action, transition=transition,
                                  operation_id="o1", task_id="t1", part_id="p1", application_name="demo-web",
                                  worker_hostname="w1") for action, transition in (("deploy", "admitted"), ("deploy", "succeeded"))]
  payload = container_payload(container, state(container), [], events[::-1])
  assert [(event["action"], event["transition"], event["operation_id"], event["task_id"], event["part_id"])
          for event in payload["events"]] == [("deploy", "succeeded", "o1", "t1", "p1"), ("deploy", "admitted", "o1", "t1", "p1")]
