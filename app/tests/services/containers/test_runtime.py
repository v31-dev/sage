from datetime import datetime, timedelta

import pytest

from services.containers.decisions import ContainerFacts, Runtime, RuntimeReading, decide_container, routable
from services.containers.record import Applied, Authority, Operation, Settled, TargetRead, TargetRecord
from services.containers.runtime import runtime_of
from services.worker_reports import ContainerReading, Received, Snapshot

ID = "0123456789abcdef"
OURS = Authority("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", ID)
RESTORED = Authority("aaaaaaaaaaaaaaaa", "cccccccccccccccc", ID)
HASH = "f" * 64
# The worker's clock runs three hours from the Manager's: the two are never compared.
MANAGER = datetime(2026, 1, 1, 12, 0, 0)
WORKER = MANAGER + timedelta(hours=3)


def labels(deployment, authority=OURS):
  return {"sage.instance": authority.instance_id, "sage.generation": authority.state_generation,
          "sage.container": authority.container_id, "sage.deployment": deployment, "sage.application": "demo-web"}


def sample(gathered: int, received: int, deployment="B", status="running", authority=OURS, listed=True) -> Received:
  """A report gathered `gathered` seconds into the worker's clock and received `received` seconds into the Manager's."""
  reading = ContainerReading(name=f"sage-{ID}", status=status, image="img", labels=labels(deployment, authority), exit_code=0,
                             oom_killed=False, started_at=None, finished_at=None, health=None, cpu_percent=None, mem_bytes=None,
                             net_rx_bytes=None, net_tx_bytes=None)
  return Received(MANAGER + timedelta(seconds=received),
                  Snapshot(WORKER + timedelta(seconds=gathered), None, {reading.name: reading} if listed else {}, None))


def deployed(verified: int, finished: int, goal="running", phase="settled") -> TargetRecord:
  """A record whose operation was verified `verified` seconds into the worker's clock and finished `finished` seconds into
  the Manager's."""
  settled_phase = phase == "settled"
  return TargetRecord(
      OURS, Applied("B", HASH, MANAGER), Settled("B", goal, "succeeded", MANAGER),
      Operation("B", "deploy" if goal == "running" else "stop", phase, MANAGER, 0,
                MANAGER + timedelta(seconds=finished) if settled_phase else None, HASH if goal == "running" else None,
                outcome="succeeded" if settled_phase else None,
                verified_on_worker_at=WORKER + timedelta(seconds=verified) if settled_phase else None))


def at(seconds: int) -> datetime:
  return MANAGER + timedelta(seconds=seconds)


def facts(received, record, now):
  return ContainerFacts(OURS, TargetRead("present", record), runtime_of(received, ID, record, now, True), desired_hash=HASH)


def view(received, record, now):
  return decide_container(facts(received, record, now))


class TestADelayedOldReport:
  """Item 30: a report gathered before a deploy's verification, delivered after it, while no later report has come."""

  def test_right_after_the_deploy_its_own_verification_makes_it_active_and_routable(self):
    record = deployed(verified=5, finished=5)
    assert (view(None, record, at(6)).headline, routable(facts(None, record, at(6)))) == ("active", True)

  def test_the_delayed_older_report_does_not_overrule_the_verification(self):
    delayed = sample(gathered=0, received=30, deployment="A")
    assert runtime_of(delayed, ID, deployed(5, 5), at(30), True) == Runtime("known", RuntimeReading("running", "B", authority=OURS))
    assert view(delayed, deployed(5, 5), at(30)).headline == "active"

  def test_once_the_verification_is_old_the_older_report_is_only_stale(self):
    delayed = sample(gathered=0, received=30, deployment="A")
    assert runtime_of(delayed, ID, deployed(5, 5), at(36), True).knowledge == "stale"
    assert view(delayed, deployed(5, 5), at(36)).headline == "checking"

  def test_the_first_report_gathered_after_the_verification_takes_over(self):
    later = sample(gathered=10, received=12)
    assert runtime_of(later, ID, deployed(5, 5), at(13), True) == Runtime(
        "known", RuntimeReading("running", "B", exit_code=0, authority=OURS))

  def test_a_report_gathered_at_the_verification_counts(self):
    assert runtime_of(sample(gathered=5, received=7, status="exited"), ID, deployed(5, 5), at(8), True).reading.state == "exited"

  def test_a_failure_after_the_verification_is_an_ordinary_error(self):
    exited = sample(gathered=10, received=12, status="exited")
    assert (view(exited, deployed(5, 5), at(13)).headline, view(exited, deployed(5, 5), at(13)).reason) == (
        "error", "unexpected_exit")


def test_a_verified_stop_reads_as_stopped():
  assert view(None, deployed(5, 5, goal="stopped"), at(6)).headline == "inactive"


def test_an_unsettled_operation_sets_no_barrier_and_reports_count_by_their_receipt():
  record = deployed(5, 5, phase="executing")
  assert runtime_of(sample(gathered=0, received=30, deployment="A"), ID, record, at(31), True).reading.deployment_id == "A"
  assert runtime_of(sample(gathered=0, received=30), ID, record, at(31), True).knowledge == "known"


def test_a_settled_operation_with_no_intentional_outcome_leaves_an_older_report_stale():
  record = TargetRecord(OURS, operation=Operation("r1", "reset", "settled", MANAGER, 0, at(5), outcome="succeeded",
                                                  verified_on_worker_at=WORKER + timedelta(seconds=5)))
  assert runtime_of(sample(gathered=0, received=6), ID, record, at(6), True).knowledge == "stale"


@pytest.mark.parametrize("received, record, expected", [
    (None, None, "unobserved"),
    (sample(gathered=0, received=0), None, "known"),
    (sample(gathered=0, received=0), None, "stale"),
], ids=["no report", "a recent report", "an old report"])
def test_without_a_record_a_report_counts_by_its_receipt(received, record, expected):
  now = at(1) if expected != "stale" else at(31)
  assert runtime_of(received, ID, record, now, None).knowledge == expected


def test_an_unreachable_worker_is_unreachable_with_its_last_reading():
  runtime = runtime_of(sample(gathered=10, received=12), ID, deployed(5, 5), at(13), False)
  assert runtime == Runtime("unreachable", RuntimeReading("running", "B", exit_code=0, authority=OURS))
  assert runtime_of(None, ID, deployed(5, 5), at(6), False).reading == RuntimeReading("running", "B", authority=OURS)


def test_a_report_that_does_not_list_the_container_reads_it_absent():
  assert runtime_of(sample(gathered=10, received=12, listed=False), ID, None, at(13), True).reading == RuntimeReading("absent")


def test_a_runtime_labelled_with_another_authority_is_an_authority_error():
  foreign = sample(gathered=10, received=12, authority=RESTORED)
  assert runtime_of(foreign, ID, deployed(5, 5), at(13), True).reading.authority == RESTORED
  assert view(foreign, deployed(5, 5), at(13)).reason == "restored"
