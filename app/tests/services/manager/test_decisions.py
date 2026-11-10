
import pytest

import config
from services.manager.decisions import worker_claim_refusal, worker_owner, worker_stack_problem
from services.worker_reports import ContainerReading, HostReading, Snapshot


def snapshot(running=config.WORKER_STACK_SERVICES, traefik_reachable=True):
  def reading(name, status):
    return ContainerReading(name=name, status=status, image="", labels={}, exit_code=0, oom_killed=False,
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
    assert worker_stack_problem(snapshot(running=("vector", "telegraf", "cloudflared", "cloudflared-discovery")), 5) == (
        "traefik not running")
    assert worker_stack_problem(snapshot(running=("vector",)), 5) == (
        "traefik, telegraf, cloudflared, cloudflared-discovery not running")

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
