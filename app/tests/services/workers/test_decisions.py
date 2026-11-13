import itertools
from datetime import datetime, timedelta

import pytest

import config
from services.worker_reports import ContainerReading, Received, Snapshot
from services.workers.decisions import (SETUPS, SSH, STATUSES, WorkerFacts, WorkerReport, addressable, decide_worker, discovery_member,
                                        reboot_outcome, report_of, workable, worker_claim_refusal, worker_owner)

GOOD = WorkerReport("known", True)
LATE = config.REPORT_STARTUP_GRACE_SECONDS + 1
REASONS = {None, "ssh_unreachable", "manager_offline", "unobserved", "owned_elsewhere", "setup_failed", "not_reporting",
           "stack_down", "traefik_down", "traefik_unchecked"}


def facts(ssh="reachable", setup="succeeded", report=GOOD, tasks=(), since=LATE):
  return WorkerFacts(ssh, setup, report, frozenset(tasks), since)


SCENARIOS = {
    "a removal under way": (facts(tasks=["remove_worker", "add_worker"]), "removing", None),
    "an addition under way": (facts(ssh="unreachable", tasks=["add_worker", "reboot_worker"]), "adding", None),
    "a reboot under way": (facts(ssh="unreachable", tasks=["reboot_worker"]), "rebooting", None),
    "its SSH server does not answer": (facts(ssh="unreachable"), "offline", "ssh_unreachable"),
    "the Manager is cut off from the tailnet": (facts(ssh="unknown"), "checking", "manager_offline"),
    "nothing looked at yet": (facts(ssh="unobserved", setup=None, report=WorkerReport("unobserved"), since=0), "checking",
                              "unobserved"),
    "answering but not set up yet": (facts(setup=None), "checking", "unobserved"),
    "another Sage's worker": (facts(setup="refused"), "not ready", "owned_elsewhere"),
    "a setup that failed while SSH answers": (facts(setup="failed"), "not ready", "setup_failed"),
    "no report yet, just after the Manager started": (facts(report=WorkerReport("unobserved"), since=5), "checking", "unobserved"),
    "no report after the startup grace": (facts(report=WorkerReport("unobserved")), "not ready", "not_reporting"),
    "reports stopped": (facts(report=WorkerReport("stale", True)), "not ready", "not_reporting"),
    "part of its stack down": (facts(report=WorkerReport("known", True, ("vector", "telegraf"))), "not ready", "stack_down"),
    "Traefik not answering": (facts(report=WorkerReport("known", False)), "not ready", "traefik_down"),
    "a report without the Traefik check": (facts(report=WorkerReport("known", None)), "not ready", "traefik_unchecked"),
    "set up, reporting, Traefik answering": (facts(), "online", None),
}


@pytest.mark.parametrize("given, status, reason", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_the_decision_tables(given, status, reason):
  view = decide_worker(given)
  assert (view.status, view.reason) == (status, reason)


def test_a_stack_down_names_its_services():
  assert decide_worker(facts(report=WorkerReport("known", True, ("vector", "telegraf")))).detail == "vector, telegraf"


@pytest.mark.parametrize("status, ready", [
    ("online", True), ("checking", None), ("not ready", False), ("offline", False), ("adding", False), ("removing", False),
    ("rebooting", False)])
def test_ready_is_known_except_while_checking(status, ready):
  from services.workers.decisions import WorkerView
  assert WorkerView(status).ready is ready


def every_report():
  yield WorkerReport("unobserved")
  for knowledge, traefik, down in itertools.product(("known", "stale"), (True, False, None), ((), ("vector",))):
    yield WorkerReport(knowledge, traefik, down)


def test_every_combination_of_facts_keeps_the_invariants():
  seen_statuses, seen_reasons = set(), set()
  task_sets = [frozenset(tasks) for size in range(4) for tasks in itertools.combinations(("remove_worker", "add_worker", "reboot_worker"), size)]
  for ssh, setup, report, tasks, since in itertools.product(SSH, SETUPS, every_report(), task_sets, (0, LATE)):
    given = WorkerFacts(ssh, setup, report, tasks, since)
    view = decide_worker(given)
    seen_statuses.add(view.status)
    seen_reasons.add(view.reason)

    assert view.status in STATUSES and view.reason in REASONS, given
    assert (view.reason is None) == (view.status in ("removing", "adding", "rebooting", "online")), given
    if tasks:
      expected = next(status for name, status in (("remove_worker", "removing"), ("add_worker", "adding"),
                                                  ("reboot_worker", "rebooting")) if name in tasks)
      assert view.status == expected, given
    elif ssh == "unreachable":
      assert (view.status, view.reason) == ("offline", "ssh_unreachable"), given
    if view.status == "online":
      assert not tasks and ssh == "reachable" and setup == "succeeded", given
      assert report == WorkerReport("known", True), given
    if view.status == "offline":
      assert not tasks and ssh == "unreachable", given
    if view.status == "checking" and view.reason == "unobserved" and ssh == "reachable" and setup == "succeeded":
      assert report.knowledge == "unobserved" and since < config.REPORT_STARTUP_GRACE_SECONDS, given

  assert seen_statuses == set(STATUSES)
  assert seen_reasons == REASONS


NOW = datetime(2026, 1, 1, 12, 0, 0)


def received(arrived_seconds_ago=0, services=config.WORKER_STACK_SERVICES, traefik=True, stopped=()):
  containers = {name: ContainerReading(name=name, status="exited" if name in stopped else "running", image="img", labels={},
                                       exit_code=0, oom_killed=False, started_at=None, finished_at=None, health=None,
                                       cpu_percent=None, mem_bytes=None, net_rx_bytes=None, net_tx_bytes=None)
                for name in services}
  return Received(NOW - timedelta(seconds=arrived_seconds_ago), Snapshot(NOW, None, containers, traefik))


class TestReportOf:
  def test_no_report_is_unobserved(self):
    assert report_of(None, NOW) == WorkerReport("unobserved")

  def test_a_recent_report_with_the_whole_stack_running(self):
    running = tuple((name, "running") for name in config.WORKER_STACK_SERVICES)
    assert report_of(received(), NOW) == WorkerReport("known", True, (), running, len(running))

  def test_it_names_the_stack_services_missing_or_not_running_in_their_order(self):
    services = tuple(name for name in config.WORKER_STACK_SERVICES if name != "telegraf")
    assert report_of(received(services=services, stopped=("traefik",)), NOW).stack_down == ("traefik", "telegraf")

  def test_it_has_each_stack_services_status_and_counts_every_container_listed(self):
    services = (*(name for name in config.WORKER_STACK_SERVICES if name != "telegraf"), "sage-0123456789abcdef",
                "sage-fedcba9876543210")
    report = report_of(received(services=services, stopped=("traefik",)), NOW)
    assert dict(report.stack) == {**{name: "running" for name in config.WORKER_STACK_SERVICES}, "traefik": "exited",
                                  "telegraf": "missing"}
    assert [name for name, _ in report.stack] == list(config.WORKER_STACK_SERVICES)
    assert report.containers == len(services)

  def test_an_old_report_is_stale_and_keeps_what_it_said(self):
    report = report_of(received(config.REPORT_STALE_SECONDS + 1, traefik=False), NOW)
    assert (report.knowledge, report.traefik_reachable, report.stack_down) == ("stale", False, ())


@pytest.mark.parametrize("boot_id, ready, outcome", [
    (None, True, "unknown"),
    ("old", True, "old_boot"),
    ("new", False, "not_ready"),
    ("new", None, "not_ready"),
    ("new", True, "verified"),
])
def test_a_reboot_is_verified_only_by_a_new_boot_and_a_ready_worker(boot_id, ready, outcome):
  assert reboot_outcome("old", boot_id, ready) == outcome


@pytest.mark.parametrize("ssh, setup, address, held, after", [
    ("reachable", "succeeded", "100.64.0.2", True, True),
    ("reachable", "succeeded", None, None, False),
    ("reachable", None, "100.64.0.2", None, False),
    ("unobserved", None, None, None, False),
    ("unknown", "succeeded", "100.64.0.2", None, None),
    ("unknown", None, None, None, None),
    ("unreachable", "succeeded", "100.64.0.2", False, False),
    ("unreachable", None, None, False, False),
    ("reachable", "failed", "100.64.0.2", False, False),
    ("reachable", "refused", "100.64.0.2", False, False),
    ("unknown", "failed", "100.64.0.2", False, False),
])
def test_discovery_points_at_a_worker_reachable_and_set_up_and_holds_one_not_known_yet_for_the_hold_unless_cut_off(
        ssh, setup, address, held, after):
  hold = config.UNKNOWN_HOLD_SECONDS
  assert (discovery_member(ssh, setup, address, hold - 1), discovery_member(ssh, setup, address, hold)) == (held, after)


def test_sage_acts_only_on_a_worker_reachable_and_set_up_since_the_manager_started():
  assert {(ssh, setup) for ssh, setup in itertools.product(SSH, SETUPS) if workable(ssh, setup)} == {("reachable", "succeeded")}


def test_a_worker_is_addressable_only_reachable_and_with_an_address():
  assert {(ssh, address) for ssh, address in itertools.product(SSH, (None, "100.64.0.2")) if addressable(ssh, address)} == {
      ("reachable", "100.64.0.2")}


class TestWorkerOwner:
  def test_the_stamp_holds_the_instance_and_its_domain(self):
    assert worker_owner({"owner": "abc123 example.test"}) == ("abc123", "example.test")

  def test_a_stamp_without_a_domain_still_names_the_instance(self):
    assert worker_owner({"owner": "abc123"}) == ("abc123", "")

  @pytest.mark.parametrize("revisions", [{}, {"infra": "x"}, {"owner": ""}])
  def test_a_worker_without_a_stamp_has_no_owner(self, revisions):
    assert worker_owner(revisions) is None


class TestWorkerClaimRefusal:
  MINE = {"owner": "me example.test"}

  def test_a_worker_nobody_owns_may_be_set_up(self):
    assert worker_claim_refusal({}, "me") is None

  def test_a_worker_of_this_sage_may_be_set_up_whatever_its_domain_says(self):
    assert worker_claim_refusal({"owner": "me old.test"}, "me") is None

  def test_a_worker_of_another_sage_is_refused_with_its_domain_and_what_to_do(self):
    refusal = worker_claim_refusal({"owner": "other other.test"}, "me")
    assert "managed by another Sage (other.test)" in refusal and "remove it from there first" in refusal

  def test_a_worker_of_another_sage_with_no_domain_is_still_refused(self):
    assert "unknown domain" in worker_claim_refusal({"owner": "other"}, "me")
