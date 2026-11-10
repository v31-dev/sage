import itertools
from dataclasses import replace
from datetime import datetime

import pytest

from services.containers.decisions import (CONFIG, HEADLINES, KNOWLEDGE, RUNNING, ContainerFacts, Correction, Runtime, RuntimeReading, correction,
                                           decide_container, lifecycle_refusal, maintenance_refusal, routable)
from services.containers.record import CONTRACTS, PHASES, READS, Applied, Authority, Operation, Settled, TargetRead, TargetRecord

AT = datetime(2026, 1, 1, 12)
OURS = Authority("instance1", "generation1", "container1")
RESTORED = Authority("instance1", "generation2", "container1")
FOREIGN = Authority("instance2", "generation1", "container1")
APPLIED = Applied("d1", "h1", AT)
RUNNING_OK = Settled("d1", "running", "succeeded", AT)
STOPPED_OK = Settled("s1", "stopped", "succeeded", AT)
FAILED = Settled("d2", "stopped", "failed", AT, "pull: timeout")
PROGRESS = {"deploy": "deploying", "stop": "stopping", "delete": "stopping", "backup_stopped": "backup",
            "backup_preserve": "backup", "restore_stopped": "restoring"}
REASONS = {None, "authority", "restored", "invalid_record", "interrupted", "backup_interrupted", "failed", "unreachable", "unexpected_running",
           "unexpected_exit", "unexpected_deployment"}


def record(**fields):
  return TargetRecord(**{"authority": OURS, **fields})


def present(**fields):
  return TargetRead("present", record(**fields))


def known(state="running", deployment="d1", authority=OURS, **fields):
  return Runtime("known", RuntimeReading(state, deployment, authority=authority, **fields))


def decide(target, runtime, live=None, desired="h1"):
  return decide_container(ContainerFacts(OURS, target, runtime, live, desired))


DEPLOYED = present(applied=APPLIED, settled=RUNNING_OK)

SCENARIOS = {
    "freshly loaded, nothing read yet": (TargetRead("unobserved"), Runtime("unobserved"), None, "h1", "checking", "unknown", None),
    "new placement, no record and no runtime": (TargetRead("absent"), known("absent", None), None, "h1", "inactive", "not_deployed", None),
    "a live deploy": (DEPLOYED, known("exited"), "deploy", "h1", "deploying", "current", None),
    "a successful deploy running": (DEPLOYED, known(), None, "h1", "active", "current", None),
    "running with saved config drift": (DEPLOYED, known(), None, "h2", "active", "dirty", None),
    "another container still runs its own older deployment": (
        present(applied=Applied("d0", "h0", AT), settled=Settled("d0", "running", "succeeded", AT)), known(deployment="d0"), None, "h1",
        "active", "dirty", None),
    "a started deploy failed": (present(applied=APPLIED, settled=FAILED), known("exited"), None, "h1", "error", "current", "failed"),
    "a stop after the failed deploy succeeded": (present(applied=APPLIED, settled=STOPPED_OK), known("exited"), None, "h1", "inactive",
                                                 "current", None),
    "a manual backup or volume restore from stopped": (present(settled=STOPPED_OK), known("absent", None), None, "h1", "inactive",
                                                       "not_deployed", None),
    "an automatic backup cut off by a restart": (
        present(applied=APPLIED, settled=RUNNING_OK, operation=Operation("b1", "backup_preserve", "executing", AT, 0)), known("exited"), None,
        "h1", "checking", "current", "backup_interrupted"),
    "a deploy cut off by a restart": (present(applied=APPLIED, settled=RUNNING_OK, operation=Operation("d3", "deploy", "executing", AT, 0)),
                                      known(), None, "h1", "error", "current", "interrupted"),
    "a live operation that owns its unfinished record": (
        present(applied=APPLIED, settled=RUNNING_OK, operation=Operation("d3", "deploy", "executing", AT, 0)), known("exited"), "deploy", "h1",
        "deploying", "current", None),
    "a deployed container that exited by itself": (DEPLOYED, known("exited", exit_code=137, oom_killed=True), None, "h1", "error",
                                                   "current", "unexpected_exit"),
    "a deployed container that is gone": (DEPLOYED, known("absent", None), None, "h1", "error", "current", "unexpected_exit"),
    "a deployed container that is paused": (DEPLOYED, known("paused"), None, "h1", "error", "current", "unexpected_exit"),
    "a stopped container found running": (present(applied=APPLIED, settled=STOPPED_OK), known(), None, "h1", "error", "current",
                                          "unexpected_running"),
    "an unhealthy deployed container": (DEPLOYED, known(health="unhealthy"), None, "h1", "active", "current", None),
    "an unreachable worker": (DEPLOYED, Runtime("unreachable", RuntimeReading("running", "d1")), None, "h1", "error", "current",
                              "unreachable"),
    "a stale report from a reachable worker": (DEPLOYED, Runtime("stale", RuntimeReading("running", "d1")), None, "h1", "checking",
                                               "current", None),
    "no record but its runtime runs": (TargetRead("absent"), known(deployment=None), None, "h1", "error", "not_deployed",
                                       "unexpected_running"),
    "an unreadable record": (TargetRead("invalid"), known(), None, "h1", "error", "unknown", "invalid_record"),
    "a record from before a restore": (TargetRead("present", TargetRecord(RESTORED, APPLIED, RUNNING_OK)), known(), None, "h1",
                                       "error", "unknown", "restored"),
    "a live deploy over a record from before a restore": (TargetRead("present", TargetRecord(RESTORED, APPLIED, RUNNING_OK)), known(),
                                                          "deploy", "h1", "error", "unknown", "restored"),
    "a runtime from before a restore still running": (DEPLOYED, known(authority=RESTORED), None, "h1", "error", "current",
                                                      "restored"),
    "a live deploy while the runtime from before a restore still runs": (DEPLOYED, known(authority=RESTORED), "deploy", "h1",
                                                                         "error", "current", "restored"),
    "another Sage's record": (TargetRead("present", TargetRecord(FOREIGN, APPLIED, RUNNING_OK)), known(), None, "h1",
                              "error", "unknown", "authority"),
    "another Sage's runtime under this Sage's record from before a restore": (
        TargetRead("present", TargetRecord(RESTORED, APPLIED, RUNNING_OK)), known(authority=FOREIGN), None, "h1",
        "error", "unknown", "authority"),
    "a stale reading of a foreign runtime": (DEPLOYED, Runtime("stale", RuntimeReading("running", "d1", authority=RESTORED)), None,
                                             "h1", "checking", "current", None),
    "another deployment running than the applied one": (DEPLOYED, known(deployment="d0"), None, "h1", "error", "current",
                                                        "unexpected_deployment"),
    "a reset after a failure": (present(applied=APPLIED, settled=FAILED, operation=Operation("r1", "reset", "executing", AT, 0)),
                                known("exited"), "reset", "h1", "error", "current", "failed"),
    "a live delete": (DEPLOYED, known(), "delete", "h1", "stopping", "current", None),
    "the record kept while its worker cannot be read": (TargetRead("unreachable", DEPLOYED.record), known(), None, "h1", "active",
                                                        "current", None),
    "no record yet and the worker cannot be read": (TargetRead("unreachable"), known(), None, "h1", "checking", "unknown", None),
    "no saved fingerprint": (DEPLOYED, known(), None, None, "active", "unknown", None),
}


@pytest.mark.parametrize("target, runtime, live, desired, headline, config, reason", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_the_decision_tables(target, runtime, live, desired, headline, config, reason):
  view = decide(target, runtime, live, desired)
  assert (view.headline, view.config, view.reason) == (headline, config, reason)


def test_an_error_keeps_its_detail():
  assert decide(present(applied=APPLIED, settled=FAILED), known("exited")).detail == "pull: timeout"
  assert decide(DEPLOYED, known("exited")).detail == "exited"


def test_a_known_runtime_needs_a_reading():
  with pytest.raises(ValueError, match="A known runtime has a reading."):
    Runtime("known")


def every_record():
  operations = [None] + [Operation("o1", contract, phase, AT, 0) for contract, phase in itertools.product(CONTRACTS, PHASES)]
  for authority, applied, settled, operation in itertools.product(
          (OURS, RESTORED, FOREIGN), (None, APPLIED), (None, RUNNING_OK, STOPPED_OK, FAILED, Settled("d2", "running", "failed", AT)),
          operations):
    yield TargetRecord(authority, applied, settled, operation)


def every_target():
  for kind in ("unobserved", "absent", "invalid", "unreachable"):
    yield TargetRead(kind)
  for each in every_record():
    yield TargetRead("present", each)
    yield TargetRead("unreachable", each)


def every_runtime():
  for state, deployment, authority in itertools.product(("running", "exited", "absent", "paused", "restarting"), ("d1", "d0"),
                                                        (OURS, RESTORED, FOREIGN, None)):
    yield known(state, deployment, authority)
  yield Runtime("stale", RuntimeReading("running", "d1"))
  yield Runtime("unreachable", RuntimeReading("running", "d1"))
  yield Runtime("unreachable")
  yield Runtime("unobserved")


def expected_config(target, desired):
  """The comparison, worked out on its own: only a readable record of this authority proves what was applied."""
  if target.record is None:
    return "not_deployed" if target.kind == "absent" else "unknown"
  if target.record.authority != OURS:
    return "unknown"
  if target.record.applied is None:
    return "not_deployed"
  return "unknown" if desired is None else ("current" if target.record.applied.config_hash == desired else "dirty")


def test_every_combination_of_facts_keeps_the_invariants():
  seen_headlines, seen_reasons = set(), set()
  for target, runtime, live in itertools.product(every_target(), every_runtime(), (None, *CONTRACTS)):
    view = decide(target, runtime, live)
    seen_headlines.add(view.headline)
    seen_reasons.add(view.reason)
    record = target.record
    others = {authority for authority in (record.authority if record else None,
                                          runtime.reading.authority if runtime.knowledge == "known" else None)} - {None, OURS}
    orphaned = record is not None and record.operation is not None and record.operation.phase != "settled" and live is None
    settled = record.settled if record else None
    facts = (target, runtime, live)

    assert view.headline in HEADLINES and view.config in CONFIG and view.reason in REASONS, facts
    unoccupied = decide(target, runtime)
    assert routable(ContainerFacts(OURS, target, runtime, live, "h1")) is (
        None if unoccupied.headline == "checking" or unoccupied.reason == "unreachable" else unoccupied.headline == "active"), facts
    assert (view.reason is not None) == (view.headline == "error") or view.reason == "backup_interrupted", facts
    if others:
      assert (view.headline, view.reason) == ("error", "authority" if FOREIGN in others else "restored"), facts
    elif live in PROGRESS:
      assert (view.headline, view.reason) == (PROGRESS[live], None), facts
    elif target.kind != "invalid" and not orphaned and settled is not None and settled.result == "failed":
      assert (view.headline, view.reason) == ("error", "failed"), facts
    elif target.kind != "invalid" and not orphaned and runtime.knowledge == "unreachable":
      assert (view.headline, view.reason) == ("error", "unreachable"), facts
    if view.headline == "active":
      assert not others and live not in PROGRESS and not orphaned and settled == RUNNING_OK, facts
      assert runtime.knowledge == "known" and runtime.reading.state == "running", facts
      assert runtime.reading.deployment_id == record.applied.deployment_id, facts
    if view.headline == "inactive":
      assert runtime.knowledge == "known" and runtime.reading.state not in ("running", "paused", "restarting"), facts
      assert settled is None or settled == STOPPED_OK, facts
    if view.headline == "checking" and view.reason is None:
      assert runtime.knowledge in ("unobserved", "stale") or (record is None and target.kind != "absent"), facts

    assert view.config == expected_config(target, "h1"), facts
    redone = [decide(target, runtime, live, "h2"), decide(target, runtime, live, None)]
    if runtime.reading is not None:
      redone.append(decide(target, replace(runtime, reading=replace(runtime.reading, health="unhealthy")), live))
    assert all((other.headline, other.reason) == (view.headline, view.reason) for other in redone), facts

  assert seen_headlines == set(HEADLINES)
  assert seen_reasons == REASONS
  assert set(KNOWLEDGE) == {runtime.knowledge for runtime in every_runtime()}
  assert set(READS) == {target.kind for target in every_target()}


@pytest.mark.parametrize("target, runtime, live, expected", [
    (DEPLOYED, known(), None, True),
    (DEPLOYED, known(), "deploy", True),
    (present(applied=APPLIED, settled=RUNNING_OK, operation=Operation("d3", "deploy", "executing", AT, 0)), known(), "deploy", False),
    (present(applied=APPLIED, settled=STOPPED_OK), known("exited"), None, False),
    (present(applied=APPLIED, settled=FAILED), known("exited"), None, False),
    (DEPLOYED, known("exited"), None, False),
    (DEPLOYED, Runtime("unreachable", known().reading), None, None),
    (DEPLOYED, Runtime("stale", known().reading), None, None),
    (TargetRead("unobserved"), Runtime("unobserved"), None, None),
], ids=["active", "a deploy before it records that it began", "a deploy that began", "stopped", "failed", "exited",
        "its worker unreachable", "its report stale", "never read"])
def test_a_container_is_routable_by_its_record_and_runtime(target, runtime, live, expected):
  assert routable(ContainerFacts(OURS, target, runtime, live, "h1")) is expected


class TestLifecycleRefusal:
  """Whether a lifecycle operation may begin, from the read of the record just taken, over every read kind, authority,
  operation phase and operation."""

  def deploy_expected(self, target):
    if target.kind in ("unobserved", "unreachable"):
      return "unreachable"
    if target.kind == "invalid":
      return "invalid_record"
    if target.record is None:
      return None
    if target.record.authority.instance_id != OURS.instance_id:
      return "authority"
    operation = target.record.operation
    return "interrupted" if operation is not None and operation.phase != "settled" else None

  def test_every_read(self):
    records = [None, *(TargetRecord(authority, APPLIED, RUNNING_OK, operation) for authority in (OURS, RESTORED, FOREIGN)
                       for operation in (None, *(Operation("o1", "deploy", phase, AT, 905) for phase in PHASES)))]
    seen = set()
    for kind in READS:
      for kept in records:
        target = TargetRead(kind, kept if kind in ("present", "unreachable") else None)
        if kind == "present" and kept is None:
          continue
        deploy = lifecycle_refusal(target, OURS, "deploy")
        assert deploy == self.deploy_expected(target) == lifecycle_refusal(target, OURS, "stop"), target
        # A Delete never waits for old work: it is refused only when Deploy is refused for the worker or the owner.
        assert lifecycle_refusal(target, OURS, "delete") == (deploy if deploy in ("unreachable", "authority") else None)
        seen.add(deploy)
    assert seen == {None, "unreachable", "invalid_record", "authority", "interrupted"}

  def test_a_settled_record_from_before_a_restore_does_not_refuse(self):
    settled = Operation("o1", "deploy", "settled", AT, 905, AT, outcome="succeeded", verified_on_worker_at=AT)
    assert lifecycle_refusal(TargetRead("present", TargetRecord(RESTORED, APPLIED, RUNNING_OK, settled)), OURS, "deploy") is None


UNSETTLED = Operation("d3", "deploy", "executing", AT, 905)

CORRECTIONS = {
    "never read": (TargetRead("unobserved"), Runtime("unobserved"), False, Correction("read")),
    "a read that failed, the worker reporting": (TargetRead("unreachable", DEPLOYED.record), known(), True, Correction("read")),
    "a read that failed, the worker silent": (TargetRead("unreachable", DEPLOYED.record), Runtime("stale"), False, None),
    "unreadable": (TargetRead("invalid"), known(), True, Correction("recover", "unreadable")),
    "a deploy that never settled": (present(applied=APPLIED, settled=RUNNING_OK, operation=UNSETTLED), known(), True,
                                    Correction("recover", "interrupted")),
    "a deploy from before a restore that never settled": (TargetRead("present", TargetRecord(RESTORED, operation=UNSETTLED)),
                                                          known(), True, Correction("recover", "interrupted")),
    "an automatic backup that never settled": (
        present(applied=APPLIED, settled=RUNNING_OK, operation=Operation("b1", "backup_preserve", "executing", AT, 930)),
        known("exited"), True, Correction("recover", "interrupted")),
    "another Sage's record": (TargetRead("present", TargetRecord(FOREIGN, APPLIED, RUNNING_OK)), known(authority=FOREIGN),
                              True, None),
    "a record from before a restore": (TargetRead("present", TargetRecord(RESTORED, APPLIED, RUNNING_OK)),
                                       known(authority=RESTORED), True, Correction("reset", "restored")),
    "a runtime from before a restore under a current record": (DEPLOYED, known(authority=RESTORED), True,
                                                               Correction("reset", "restored")),
    "running after a stop": (present(applied=APPLIED, settled=STOPPED_OK), known(), True,
                             Correction("reset", "unexpected_running")),
    "running with no outcome": (present(), known(), True, Correction("reset", "unexpected_running")),
    "running another deployment": (DEPLOYED, known(deployment="d0"), True, Correction("reset", "unexpected_deployment")),
    "running its deployment": (DEPLOYED, known(), True, None),
    "exited after its deployment": (DEPLOYED, known("exited"), True, None),
    "running without a record": (TargetRead("absent"), known(deployment="d0"), True, Correction("reset", "no_record")),
    "another Sage's runtime without a record": (TargetRead("absent"), known(authority=FOREIGN), True, None),
    "a stale runtime after a stop": (present(applied=APPLIED, settled=STOPPED_OK), Runtime("stale", RuntimeReading("running", "d1", authority=OURS)),
                                     True, None),
}


@pytest.mark.parametrize("target, runtime, reporting, expected", CORRECTIONS.values(), ids=CORRECTIONS.keys())
def test_the_correction_table(target, runtime, reporting, expected):
  assert correction(target, runtime, None, OURS, reporting) == expected


def test_every_combination_of_facts_gets_the_correction_its_rules_give():
  seen = set()
  for target, runtime, live, reporting in itertools.product(every_target(), every_runtime(), (None, *CONTRACTS), (True, False)):
    result = correction(target, runtime, live, OURS, reporting)
    seen.add(result)
    record = target.record if target.kind == "present" else None
    reading = runtime.reading if runtime.knowledge == "known" else None
    labelled_ours = reading is not None and reading.authority is not None and reading.authority.instance_id == OURS.instance_id
    unsettled = record is not None and record.operation is not None and record.operation.phase != "settled"
    facts = (target, runtime, live, reporting)

    if live is not None:
      assert result is None, facts
    elif target.kind == "unobserved":
      assert result == Correction("read"), facts
    elif not reporting:
      assert result is None, facts
    elif target.kind == "unreachable":
      assert result == Correction("read"), facts
    elif record is not None and record.authority.instance_id != OURS.instance_id:
      assert result is None, facts
    elif target.kind == "invalid" or unsettled:
      assert result is not None and result.kind == "recover", facts
    elif record is not None and record.authority != OURS:
      assert result == Correction("reset", "restored"), facts
    elif not (reading is not None and reading.state in RUNNING and labelled_ours):
      assert result is None, facts
    else:
      agrees = (record is not None and reading.authority == OURS and record.settled is not None
                and record.settled.runtime_goal == "running" and record.applied is not None
                and reading.deployment_id == record.applied.deployment_id)
      assert (result is None) == agrees and (result is None or result.kind == "reset"), facts
  assert {result.kind for result in seen if result} == {"read", "recover", "reset"}
  assert {result.cause for result in seen if result and result.kind == "reset"} == {
      "restored", "unexpected_running", "unexpected_deployment", "no_record"}


MAINTENANCE = {
    "a manual backup of a stopped container": ("backup_stopped", STOPPED_OK, APPLIED, RuntimeReading("absent"), None),
    "a manual backup after a failure that left it stopped": ("backup_stopped", FAILED, APPLIED, RuntimeReading("exited"), None),
    "a manual backup of a running container": ("backup_stopped", RUNNING_OK, APPLIED, RuntimeReading("running", "d1"), "running"),
    "a manual backup of one stopped by its record that runs": ("backup_stopped", STOPPED_OK, APPLIED, RuntimeReading("running", "d1"),
                                                               "running"),
    "a manual backup of one never deployed": ("backup_stopped", None, None, RuntimeReading("absent"), "not_deployed"),
    "an automatic backup of one running its deployment": ("backup_preserve", RUNNING_OK, APPLIED, RuntimeReading("running", "d1"), None),
    "an automatic backup of one stopped": ("backup_preserve", STOPPED_OK, APPLIED, RuntimeReading("absent"), None),
    "an automatic backup after a failure": ("backup_preserve", FAILED, APPLIED, RuntimeReading("exited"), "failed"),
    "an automatic backup of one that exited": ("backup_preserve", RUNNING_OK, APPLIED, RuntimeReading("exited", "d1"), "unexpected_runtime"),
    "an automatic backup of one running another deployment": ("backup_preserve", RUNNING_OK, APPLIED, RuntimeReading("running", "d0"),
                                                              "unexpected_runtime"),
    "an automatic backup of one stopped that runs": ("backup_preserve", STOPPED_OK, APPLIED, RuntimeReading("running", "d1"),
                                                     "unexpected_runtime"),
    "an automatic backup whose runtime is not known": ("backup_preserve", STOPPED_OK, APPLIED, None, "unexpected_runtime"),
    "an automatic backup of one never deployed": ("backup_preserve", None, None, RuntimeReading("absent"), "not_deployed"),
    "a restore into a stopped container": ("restore_stopped", STOPPED_OK, APPLIED, RuntimeReading("absent"), None),
    "a restore into one never deployed": ("restore_stopped", None, None, RuntimeReading("absent"), None),
    "a restore into a running container": ("restore_stopped", RUNNING_OK, APPLIED, RuntimeReading("running", "d1"), "running"),
    "a restore into one never deployed that runs": ("restore_stopped", None, None, RuntimeReading("running", "d1"), "running"),
}


@pytest.mark.parametrize("contract, settled, applied, reading, expected", MAINTENANCE.values(), ids=MAINTENANCE.keys())
def test_the_backup_refusal_table(contract, settled, applied, reading, expected):
  assert maintenance_refusal(contract, settled, applied, reading) == expected


def test_every_combination_gets_the_backup_refusal_its_rules_give():
  readings = [None, *(RuntimeReading(state, deployment) for state in ("absent", "exited", "created", *RUNNING) for deployment in (None, "d0", "d1"))]
  for contract, settled, applied, reading in itertools.product(
          ("backup_stopped", "backup_preserve", "restore_stopped"), (None, RUNNING_OK, STOPPED_OK, FAILED, Settled("d1", "running", "failed", AT)),
          (None, APPLIED), readings):
    result = maintenance_refusal(contract, settled, applied, reading)
    running = reading is not None and reading.state in RUNNING
    facts = (contract, settled, applied, reading)
    if contract == "restore_stopped":
      assert result == ("running" if running or (settled is not None and settled.runtime_goal == "running") else None), facts
    elif settled is None:
      assert result == "not_deployed", facts
    elif contract == "backup_stopped":
      assert result == (None if settled.runtime_goal == "stopped" and not running else "running"), facts
    elif settled.result == "failed":
      assert result == "failed", facts
    else:
      kept = (running and applied is not None and reading.deployment_id == applied.deployment_id if settled.runtime_goal == "running"
              else reading is not None and not running)
      assert result == (None if kept else "unexpected_runtime"), facts
