import itertools
from datetime import datetime, timedelta

import pytest

import config
from services.worker_reports import ContainerReading, Received, Snapshot
from services.workers.decisions import SETUPS, SSH, STATUSES, WorkerFacts, WorkerReport, decide_worker, reboot_outcome, report_of

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
    assert report_of(received(), NOW) == WorkerReport("known", True, ())

  def test_it_names_the_stack_services_missing_or_not_running_in_their_order(self):
    services = tuple(name for name in config.WORKER_STACK_SERVICES if name != "telegraf")
    assert report_of(received(services=services, stopped=("traefik",)), NOW).stack_down == ("traefik", "telegraf")

  def test_an_old_report_is_stale_and_keeps_what_it_said(self):
    assert report_of(received(config.REPORT_STALE_SECONDS + 1, traefik=False), NOW) == WorkerReport("stale", False, ())


@pytest.mark.parametrize("boot_id, ready, outcome", [
    (None, True, "unknown"),
    ("old", True, "old_boot"),
    ("new", False, "not_ready"),
    ("new", None, "not_ready"),
    ("new", True, "verified"),
])
def test_a_reboot_is_verified_only_by_a_new_boot_and_a_ready_worker(boot_id, ready, outcome):
  assert reboot_outcome("old", boot_id, ready) == outcome
