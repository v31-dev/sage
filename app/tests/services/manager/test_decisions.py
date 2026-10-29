import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import config
from services.manager.decisions import (ABSENT, OFFLINE, SEEN, UNKNOWN, WORKER_TASKS, Observation, WorkerInputs, WorkerReboot, WorkerRow,
                                        is_worker_rebooting, plan_worker_sync, reconcile_container, rollup_status, worker_claim_refusal, worker_entry,
                                        worker_owner, worker_ready, worker_stack_problem, worker_status, worker_with_row, worker_without_row)
from services.worker_reports import ContainerReading, HostReading, Received, Snapshot

STAMP = "2026-10-24T23:08:35.718155"


def seen(state, label=STAMP):
  return Observation(SEEN, state, label)


def reconcile(status, observation, stamp=STAMP):
  return reconcile_container("w1-demo-web", status, observation, stamp)


class TestReconcileContainer:
  @pytest.mark.parametrize("status", ["active", "inactive", "error"])
  def test_a_container_that_could_not_be_looked_at_keeps_its_status(self, status):
    assert reconcile(status, Observation(UNKNOWN)).status is None

  def test_an_active_container_on_a_worker_that_is_out_of_the_pool_is_an_error(self):
    result = reconcile("active", Observation(OFFLINE))
    assert result.status == "error" and "worker offline" in result.message and result.level == "error"

  @pytest.mark.parametrize("status", ["inactive", "error"])
  def test_a_container_that_was_not_active_stays_as_it_is_when_it_is_not_running(self, status):
    assert reconcile(status, Observation(OFFLINE)).status is None
    assert reconcile(status, Observation(ABSENT)).status is None

  def test_an_active_container_that_is_gone_is_an_error(self):
    result = reconcile("active", Observation(ABSENT))
    assert result.status == "error" and "not running" in result.message

  @pytest.mark.parametrize("state", ["exited", "created", "dead", "removing"])
  def test_an_active_container_in_a_stopped_state_is_an_error_that_names_the_state(self, state):
    result = reconcile("active", seen(state))
    assert result.status == "error" and state in result.message

  @pytest.mark.parametrize("state", ["paused", "restarting"])
  def test_an_active_container_that_is_paused_or_restarting_is_an_error(self, state):
    result = reconcile("active", seen(state))
    assert result.status == "error" and state in result.message

  @pytest.mark.parametrize("state", ["paused", "restarting"])
  def test_a_container_already_in_error_is_not_reported_again(self, state):
    assert reconcile("error", seen(state)).status is None

  @pytest.mark.parametrize("status", ["inactive", "error"])
  def test_a_container_that_is_running_the_current_version_is_active_again(self, status):
    result = reconcile(status, seen("running"))
    assert result.status == "active" and result.level == "success" and not result.stop_stale

  def test_an_active_container_running_the_current_version_needs_nothing(self):
    result = reconcile("active", seen("running"))
    assert result.status is None and result.message is None

  @pytest.mark.parametrize("status", ["active", "inactive", "error"])
  def test_a_running_container_of_another_version_is_stopped_and_becomes_inactive(self, status):
    result = reconcile(status, seen("running", label="older"))
    assert result.stop_stale and result.status == "inactive" and result.level == "error"

  def test_a_running_container_with_no_label_is_of_another_version(self):
    assert reconcile("active", seen("running", label="")).stop_stale


class TestRollupStatus:
  @pytest.mark.parametrize("statuses, expected", [
      (["error"], "error"),
      (["active", "error", "inactive"], "error"),
      (["active", "active"], "active"),
      (["inactive", "inactive"], "inactive"),
      (["active", "inactive"], None),
      ([], None),
  ])
  def test_table(self, statuses, expected):
    assert rollup_status(statuses) == expected

  def test_it_takes_any_iterable(self):
    assert rollup_status(s for s in ("active", "active")) == "active"


def row(name, online, ip="100.64.0.2"):
  return WorkerRow(name, ip, online)


def plan(known, peers, force=False, check_stamps=False):
  return [(a.kind, a.worker.hostname, a.reason, a.force) for a in plan_worker_sync(known, peers, force, check_stamps)]


class TestPlanWorkerSync:
  def test_a_worker_that_stopped_answering_is_taken_out(self):
    assert plan([row("w1", True)], [row("w1", False)]) == [("offline", "w1", "went offline", False)]

  def test_a_worker_that_answers_again_is_set_up(self):
    assert plan([row("w1", False)], [row("w1", True)]) == [("setup", "w1", "came back online", False)]

  def test_a_worker_with_another_address_is_set_up(self):
    result = plan([row("w1", True, "100.64.0.2")], [row("w1", True, "100.64.0.9")])
    assert result == [("setup", "w1", "IP changed from 100.64.0.2 to 100.64.0.9", False)]

  def test_a_worker_with_another_address_that_does_not_answer_waits(self):
    assert plan([row("w1", False, "100.64.0.2")], [row("w1", False, "100.64.0.9")]) == []

  def test_nothing_happens_to_a_worker_that_is_as_it_was(self):
    assert plan([row("w1", True), row("w2", False)], [row("w1", True), row("w2", False)]) == []

  def test_a_forced_sync_sets_up_the_workers_that_answer_from_scratch(self):
    result = plan([row("w1", True), row("w2", False)], [row("w1", True), row("w2", False)], force=True)
    assert result == [("setup", "w1", "force sync", True)]

  def test_the_stamp_check_sets_up_the_workers_that_answer(self):
    result = plan([row("w1", True), row("w2", False)], [row("w1", True), row("w2", False)], check_stamps=True)
    assert result == [("setup", "w1", "stamp check", False)]

  def test_a_worker_the_tailnet_no_longer_lists_is_taken_out_if_it_was_online(self):
    result = plan([row("w1", True), row("w2", False)], [])
    assert result == [("offline", "w1", "not found in the tailnet", False)]

  def test_a_worker_that_just_came_back_is_not_also_forced(self):
    result = plan([row("w1", False)], [row("w1", True)], force=True)
    assert result == [("setup", "w1", "came back online", False)]

  def test_a_worker_not_known_yet_that_answers_is_set_up(self):
    assert plan([row("w1", None)], [row("w1", True)]) == [("setup", "w1", "found online", False)]
    assert plan([row("w1", None)], [row("w1", True)], check_stamps=True) == [("setup", "w1", "found online", False)]

  def test_a_worker_not_known_yet_that_is_found_offline_is_taken_out(self):
    assert plan([row("w1", None)], [row("w1", False)]) == [("offline", "w1", "found offline", False)]

  def test_a_worker_still_not_known_is_left_alone(self):
    assert plan([row("w1", None)], [row("w1", None)], force=True, check_stamps=True) == []

  def test_a_worker_not_known_yet_that_the_tailnet_does_not_list_is_taken_out(self):
    assert plan([row("w1", None)], []) == [("offline", "w1", "not found in the tailnet", False)]


def snapshot(running=("traefik", "vector", "telegraf", "cloudflared"), traefik_reachable=True):
  def reading(name, status):
    return ContainerReading(name=name, status=status, image="", deployed_at=None, exit_code=0, oom_killed=False,
                            started_at=1, finished_at=None, health=None, cpu_percent=None, mem_bytes=None,
                            net_rx_bytes=None, net_tx_bytes=None)
  from datetime import datetime
  host = HostReading(*([None] * 13))
  return Snapshot(at=datetime(2026, 1, 1), host=host, containers={n: reading(n, "running") for n in running},
                  traefik_reachable=traefik_reachable)


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


class TestWorkerStackProblem:
  def test_a_stack_that_is_all_running_and_answering_has_none(self):
    assert worker_stack_problem(snapshot(), 5) is None

  def test_a_missing_or_stopped_container_of_the_stack_is_named(self):
    assert worker_stack_problem(snapshot(running=("vector", "telegraf", "cloudflared")), 5) == "traefik not running"
    assert worker_stack_problem(snapshot(running=("vector",)), 5) == "traefik, telegraf, cloudflared not running"

  def test_a_stopped_state_counts_as_not_running(self):
    from dataclasses import replace
    shot = snapshot()
    shot.containers["traefik"] = replace(shot.containers["traefik"], status="exited")
    assert worker_stack_problem(shot, 5) == "traefik not running"

  def test_traefik_running_but_not_answering_is_a_problem(self):
    assert worker_stack_problem(snapshot(traefik_reachable=False), 5) == "Traefik is not answering"

  def test_a_report_without_the_check_is_not_a_problem(self):
    assert worker_stack_problem(snapshot(traefik_reachable=None), 5) is None

  def test_no_report_is_a_problem_only_after_a_silence(self):
    assert worker_stack_problem(None, config.WORKER_STACK_SILENCE_SECONDS) is None
    assert worker_stack_problem(None, config.WORKER_STACK_SILENCE_SECONDS + 1) == "not reporting"


class TestIsWorkerRebooting:
  ASKED = datetime(2026, 1, 1, 12)

  def under_way(self, started_at, seconds=30, before=100):
    mark = WorkerReboot(self.ASKED, before)
    return is_worker_rebooting(mark, self.ASKED + timedelta(seconds=seconds), started_at)

  def test_a_worker_with_no_current_report_is_still_rebooting(self):
    assert self.under_way(None)

  def test_a_report_with_the_same_telegraf_start_is_from_before_the_host_went_down(self):
    assert self.under_way(100)

  def test_a_report_from_a_telegraf_that_restarted_means_the_worker_is_back(self):
    assert not self.under_way(200)

  @pytest.mark.parametrize("started_at", [200, None])
  def test_with_nothing_to_compare_with_any_report_counts_as_back(self, started_at):
    mark = WorkerReboot(self.ASKED, None)
    assert is_worker_rebooting(mark, self.ASKED + timedelta(seconds=30), started_at) is (started_at is None)

  def test_a_worker_that_never_comes_back_stops_being_one_after_the_limit(self):
    assert self.under_way(None, seconds=config.WORKER_REBOOT_LIMIT_SECONDS)
    assert not self.under_way(None, seconds=config.WORKER_REBOOT_LIMIT_SECONDS + 1)
    assert not self.under_way(100, seconds=config.WORKER_REBOOT_LIMIT_SECONDS + 1)


NOW = datetime(2026, 1, 1, 12)


def report(arrived_at=NOW, traefik_reachable=True, started_at=100, running=("traefik", "telegraf")):
  """A worker's report that arrived at `arrived_at`, with Telegraf started at `started_at` on the worker's clock."""
  shot = snapshot(running=running, traefik_reachable=traefik_reachable)
  containers = {name: replace(reading, started_at=started_at) if name == "telegraf" else reading
                for name, reading in shot.containers.items()}
  return Received(arrived_at, replace(shot, containers=containers))


def status(now=NOW, started=NOW, **inputs):
  return worker_status(WorkerInputs(ip="100.64.0.2", **inputs), now, started)


def later(seconds):
  return NOW + timedelta(seconds=seconds)


class TestWorkerStatus:
  @pytest.mark.parametrize("task, expected", [("add_worker", "adding"), ("remove_worker", "removing"), ("reboot_worker", "rebooting")])
  def test_each_task_the_queue_is_watched_for_gives_the_worker_a_status(self, task, expected):
    assert status(online=True, tasks=frozenset({task})) == expected
    assert status(online=False, tasks=frozenset({task})) == expected
    assert status(online=None, tasks=frozenset({task})) == expected

  def test_the_tasks_are_the_ones_the_statuses_are_given_for(self):
    assert WORKER_TASKS == ("add_worker", "remove_worker", "reboot_worker")

  def test_a_worker_that_answers_and_is_not_known_to_be_otherwise_is_online(self):
    assert status(online=True) == "online"

  def test_a_worker_that_does_not_answer_is_offline(self):
    assert status(online=False) == "offline"

  def test_a_worker_whose_traefik_does_not_answer_is_not_ready(self):
    assert status(online=True, report=report(traefik_reachable=False)) == "not ready"

  def test_a_worker_that_does_not_report_is_not_ready_once_the_manager_has_been_up_a_while(self):
    assert status(online=True, now=later(config.REPORT_STARTUP_GRACE_SECONDS)) == "not ready"

  def test_a_worker_that_does_not_report_is_online_just_after_the_manager_started(self):
    assert status(online=True, now=later(config.REPORT_STARTUP_GRACE_SECONDS - 1)) == "online"

  def test_a_worker_whose_report_went_stale_is_not_ready(self):
    assert status(online=True, report=report(), now=later(config.REPORT_STALE_SECONDS)) == "online"
    assert status(online=True, report=report(), now=later(config.REPORT_STALE_SECONDS + 1)) == "not ready"

  def test_a_worker_that_does_not_answer_is_offline_whatever_its_report_says(self):
    assert status(online=False, report=report(traefik_reachable=False)) == "offline"

  def test_a_worker_no_sync_has_looked_at_is_checking_whatever_its_report_says(self):
    assert status() == "checking"
    assert status(online=None, report=report()) == "checking"
    assert status(online=None, report=report(traefik_reachable=False)) == "checking"

  def test_removing_wins_over_rebooting_and_adding(self):
    assert status(online=True, tasks=frozenset(WORKER_TASKS)) == "removing"
    assert status(online=True, tasks=frozenset({"add_worker", "reboot_worker"})) == "adding"

  def test_after_the_task_a_reboot_lasts_until_the_worker_reports_with_a_telegraf_that_restarted(self):
    mark = WorkerReboot(NOW, 100)
    assert status(online=True, reboot=mark, report=report(started_at=100)) == "rebooting"      # reports go on until the host goes down
    assert status(online=False, reboot=mark, report=report(started_at=100), now=later(config.REPORT_STALE_SECONDS + 1)) == "rebooting"
    assert status(online=True, reboot=mark, report=report(arrived_at=later(60), started_at=100 + 90_000_000_000),
                  now=later(60)) == "online"

  def test_a_worker_that_is_back_is_not_rebooting_while_its_traefik_does_not_answer(self):
    mark = WorkerReboot(NOW, 100)
    assert status(online=True, reboot=mark, report=report(started_at=200, traefik_reachable=False)) == "not ready"

  def test_a_worker_that_does_not_come_back_is_no_longer_rebooting_after_the_limit(self):
    mark = WorkerReboot(NOW, None)
    assert status(online=False, reboot=mark, now=later(config.WORKER_REBOOT_LIMIT_SECONDS)) == "rebooting"
    assert status(online=False, reboot=mark, now=later(config.WORKER_REBOOT_LIMIT_SECONDS + 1)) == "offline"


class TestWorkerReady:
  def ready(self, now=NOW, started=NOW, **inputs):
    return worker_ready(WorkerInputs(**inputs), now, started)

  def test_a_worker_reporting_with_traefik_answering_is_ready(self):
    assert self.ready(report=report())

  def test_traefik_not_answering_is_not_ready(self):
    assert not self.ready(report=report(traefik_reachable=False))

  def test_a_report_without_the_check_is_ready(self):
    assert self.ready(report=report(traefik_reachable=None))

  def test_a_stale_report_is_not_ready(self):
    assert not self.ready(report=report(), now=later(config.REPORT_STALE_SECONDS + 1))

  def test_a_worker_that_has_not_reported_is_ready_for_the_grace_after_the_start_and_not_after(self):
    assert self.ready(now=later(config.REPORT_STARTUP_GRACE_SECONDS - 1))
    assert not self.ready(now=later(config.REPORT_STARTUP_GRACE_SECONDS))


class TestWorkerEntry:
  def entry(self, now=NOW, **inputs):
    return worker_entry("w1", WorkerInputs(ip="100.64.0.2", **inputs), now, NOW)

  def test_it_holds_what_the_list_shows_of_the_worker(self):
    assert self.entry(online=True, path="direct", report=report(running=("traefik", "web", "telegraf"))) == {
        "hostname": "w1", "ip": "100.64.0.2", "online": True, "status": "online", "path": "direct", "container_count": 3,
        "infra": [{"name": "traefik", "status": "running"}, {"name": "vector", "status": "missing"},
                  {"name": "telegraf", "status": "running"}, {"name": "cloudflared", "status": "missing"}]}

  def test_the_container_count_and_the_stack_are_those_of_the_current_report_only(self):
    assert self.entry(report=report())["container_count"] == 2
    assert self.entry(report=report())["infra"] is not None
    stale = self.entry(report=report(), now=later(config.REPORT_STALE_SECONDS + 1))
    assert (stale["container_count"], stale["infra"]) == (None, None)
    assert (self.entry()["container_count"], self.entry()["infra"]) == (None, None)


class TestWorkerRows:
  def test_a_worker_that_gets_a_row_starts_not_known_to_be_online_and_not_rebooting(self):
    kept = WorkerInputs(online=True, reboot=WorkerReboot(NOW, 1), tasks=frozenset({"add_worker"}), report=report())
    row = worker_with_row(kept, "100.64.0.2", "direct")
    assert (row.ip, row.online, row.reboot, row.path) == ("100.64.0.2", None, None, "direct")
    assert row.tasks == kept.tasks and row.report == kept.report

  def test_a_worker_that_had_a_row_keeps_what_it_has_when_the_row_is_written_again(self):
    held = WorkerInputs(ip="100.64.0.2", online=True, reboot=WorkerReboot(NOW, 1))
    row = worker_with_row(held, "100.64.0.9", None)
    assert (row.ip, row.online, row.reboot) == ("100.64.0.9", True, held.reboot)

  def test_a_worker_whose_row_is_gone_keeps_only_its_tasks(self):
    held = WorkerInputs(ip="100.64.0.2", online=True, tasks=frozenset({"remove_worker"}), report=report(), path="direct")
    assert worker_without_row(held) == WorkerInputs(tasks=frozenset({"remove_worker"}))
