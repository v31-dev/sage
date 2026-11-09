import itertools
from dataclasses import replace
from datetime import datetime

import pytest

from services.containers.decisions import CONFIG, HEADLINES, KNOWLEDGE, ContainerFacts, Runtime, RuntimeReading, decide_container
from services.containers.record import CONTRACTS, PHASES, READS, Applied, Authority, Operation, Settled, TargetRead, TargetRecord

AT = datetime(2026, 1, 1, 12)
OURS = Authority("instance1", "generation1", "container1")
RESTORED = Authority("instance1", "generation2", "container1")
APPLIED = Applied("d1", "h1", AT)
RUNNING_OK = Settled("d1", "running", "succeeded", AT)
STOPPED_OK = Settled("s1", "stopped", "succeeded", AT)
FAILED = Settled("d2", "stopped", "failed", AT, "pull: timeout")
PROGRESS = {"deploy": "deploying", "stop": "stopping", "delete": "stopping", "backup_stopped": "backup",
            "backup_preserve": "backup", "restore_stopped": "restoring"}
REASONS = {None, "authority", "invalid_record", "interrupted", "backup_interrupted", "failed", "unreachable", "unexpected_running",
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
                                       "error", "unknown", "authority"),
    "a live deploy over a record from before a restore": (TargetRead("present", TargetRecord(RESTORED, APPLIED, RUNNING_OK)), known(),
                                                          "deploy", "h1", "error", "unknown", "authority"),
    "a runtime from before a restore still running": (DEPLOYED, known(authority=RESTORED), None, "h1", "error", "current",
                                                      "authority"),
    "a live deploy while the runtime from before a restore still runs": (DEPLOYED, known(authority=RESTORED), "deploy", "h1",
                                                                         "error", "current", "authority"),
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
          (OURS, RESTORED), (None, APPLIED), (None, RUNNING_OK, STOPPED_OK, FAILED, Settled("d2", "running", "failed", AT)), operations):
    yield TargetRecord(authority, applied, settled, operation)


def every_target():
  for kind in ("unobserved", "absent", "invalid", "unreachable"):
    yield TargetRead(kind)
  for each in every_record():
    yield TargetRead("present", each)
    yield TargetRead("unreachable", each)


def every_runtime():
  for state, deployment, authority in itertools.product(("running", "exited", "absent", "paused", "restarting"), ("d1", "d0"),
                                                        (OURS, RESTORED, None)):
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
    foreign = (record is not None and record.authority != OURS) or (
        runtime.knowledge == "known" and runtime.reading.authority not in (None, OURS))
    orphaned = record is not None and record.operation is not None and record.operation.phase != "settled" and live is None
    settled = record.settled if record else None
    facts = (target, runtime, live)

    assert view.headline in HEADLINES and view.config in CONFIG and view.reason in REASONS, facts
    assert view.routable == (view.headline == "active"), facts
    assert (view.reason is not None) == (view.headline == "error") or view.reason == "backup_interrupted", facts
    if foreign:
      assert (view.headline, view.reason) == ("error", "authority"), facts
    elif live in PROGRESS:
      assert (view.headline, view.reason) == (PROGRESS[live], None), facts
    elif target.kind != "invalid" and not orphaned and settled is not None and settled.result == "failed":
      assert (view.headline, view.reason) == ("error", "failed"), facts
    elif target.kind != "invalid" and not orphaned and runtime.knowledge == "unreachable":
      assert (view.headline, view.reason) == ("error", "unreachable"), facts
    if view.headline == "active":
      assert not foreign and live not in PROGRESS and not orphaned and settled == RUNNING_OK, facts
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
