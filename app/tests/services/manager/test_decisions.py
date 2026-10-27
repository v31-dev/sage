from datetime import datetime, timedelta

import pytest

import config
from services.manager.decisions import (ABSENT, OFFLINE, SEEN, UNKNOWN, Observation, WorkerReboot, WorkerRow, is_worker_rebooting, plan_worker_sync,
                                        reconcile_container, rollup_status, worker_claim_refusal, worker_owner, worker_stack_problem)
from services.worker_reports import ContainerReading, HostReading, Snapshot

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
