from datetime import datetime, timedelta

import httpx
import pytest

import config
from services.certs import TEMPLATE_DIGEST, Certs
from services.cloudflare import Cloudflare
from services.db.models import Worker
from services.tailscale import Tailscale
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from services.workers.decisions import WorkerReport, WorkerView
from services.workers.state import Workers, WorkerState, worker_payload, worker_state
from tests.services.test_cloudflare import ACCOUNT, TUNNELS, Api, ok


class Observed:
  """What the workers' observation found: each worker's SSH fact, address and path."""

  def __init__(self):
    self.ssh_of, self.addresses, self.paths = {}, {}, {}

  def ssh(self, hostname):
    return self.ssh_of.get(hostname, "unobserved")

  def address(self, hostname):
    return self.addresses.get(hostname)

  def path(self, address):
    return self.paths.get(address)


class Catalog:
  """The certificates: what each worker's state shows of them, by the names of its subdomain and what it applied."""

  def worker_state(self, hostname, subdomain, applied, template):
    return {"worker": {"for": (hostname, subdomain, applied, template)}}


@pytest.fixture
def observed(register, clock):
  register(Certs, Catalog())
  return register(Tailscale, Observed())


def report(hostname="w1", traefik=True, running=config.WORKER_STACK_SERVICES):
  readings = {name: ContainerReading(name=name, status="running", image="", labels={}, exit_code=0, oom_killed=False, started_at=1,
                                     finished_at=None, health=None, cpu_percent=None, mem_bytes=None, net_rx_bytes=None,
                                     net_tx_bytes=None) for name in running}
  WorkerReports().accept(hostname, [Snapshot(at=datetime(2026, 1, 1, 9), host=HostReading(*([None] * 13)), containers=readings,
                                             traefik_reachable=traefik)])


class TestWorkers:
  def test_a_worker_has_no_setup_outcome_until_one_is_recorded_and_none_again_once_cleared(self):
    assert Workers().setup("w1") is None
    Workers().record_setup("w1", "succeeded")
    assert Workers().setup("w1") == "succeeded"
    Workers().record_setup("w1", None)
    assert Workers().setup("w1") is None

  def test_the_certificates_a_worker_applied_are_none_until_recorded_and_a_removal_forgets_them_with_its_setup(self):
    assert Workers().certificates("w1") is None
    Workers().record_certificates("w1", {"worker": "p1"})
    Workers().record_setup("w1", "succeeded")
    Workers().record_setup("w1", None)
    assert Workers().certificates("w1") == {"worker": "p1"}
    Workers().record_setup("w1", "succeeded")
    Workers().forget("w1")
    assert (Workers().certificates("w1"), Workers().setup("w1")) == (None, None)


class TestWorkerState:
  def test_a_reachable_worker_set_up_and_reporting_a_working_stack_is_online_with_its_address_and_path(self, observed):
    observed.ssh_of["w1"], observed.addresses["w1"], observed.paths["100.64.0.2"] = "reachable", "100.64.0.2", "direct"
    Workers().record_setup("w1", "succeeded")
    report()
    state = worker_state("w1", "red", frozenset())
    assert (state.view.status, state.view.ready, state.address, state.path, state.ssh, state.setup) == (
        "online", True, "100.64.0.2", "direct", "reachable", "succeeded")

  def test_its_certificates_are_judged_against_its_subdomain_and_what_it_applied(self, observed):
    Workers().record_certificates("w1", {"worker": "p1"})
    assert worker_state("w1", "red", frozenset()).certificates == {"worker": {"for": ("w1", "red", {"worker": "p1"}, TEMPLATE_DIGEST)}}

  def test_its_tunnel_is_the_health_cloudflare_last_listed_for_its_own_tunnel(self, observed, register):
    api = Api()
    api.on("GET", TUNNELS, ok([{"id": "t1", "name": "sage-example.test-w1", "status": "degraded"},
                               {"id": "t2", "name": "sage-other.test-w2", "status": "down"}]))
    cloudflare = register(Cloudflare, Cloudflare())
    cloudflare.http.close()
    cloudflare.http = httpx.Client(base_url="https://cloudflare.test", transport=httpx.MockTransport(api.handle))
    cloudflare.zone_id, cloudflare.account_id = "zone1", ACCOUNT
    assert worker_state("w1", "red", frozenset()).tunnel is None
    cloudflare.tunnels()
    cloudflare.http.close()
    assert (worker_state("w1", "red", frozenset()).tunnel, worker_state("w2", "blue", frozenset()).tunnel) == ("degraded", None)

  def test_a_worker_never_observed_is_checking(self, observed):
    assert worker_state("w1", "red", frozenset()).view.status == "checking"

  def test_a_reachable_worker_not_set_up_yet_is_checking(self, observed):
    observed.ssh_of["w1"] = "reachable"
    report()
    assert (worker_state("w1", "red", frozenset()).view.status, worker_state("w1", "red", frozenset()).view.reason) == ("checking", "unobserved")

  def test_a_worker_whose_ssh_server_does_not_answer_is_offline(self, observed):
    observed.ssh_of["w1"] = "unreachable"
    assert worker_state("w1", "red", frozenset()).view.status == "offline"

  def test_a_task_under_way_is_what_it_shows(self, observed):
    observed.ssh_of["w1"] = "reachable"
    assert worker_state("w1", "red", frozenset({"reboot_worker"})).view.status == "rebooting"

  def test_a_worker_that_never_reported_is_not_ready_once_the_grace_after_the_start_is_over(self, observed, clock):
    observed.ssh_of["w1"] = "reachable"
    Workers().record_setup("w1", "succeeded")
    WorkerReports()
    assert worker_state("w1", "red", frozenset()).view.status == "checking"
    clock.advance(timedelta(seconds=config.REPORT_STARTUP_GRACE_SECONDS))
    assert (worker_state("w1", "red", frozenset()).view.status, worker_state("w1", "red", frozenset()).view.reason) == ("not ready", "not_reporting")

  def test_traefik_not_answering_makes_it_not_ready(self, observed):
    observed.ssh_of["w1"] = "reachable"
    Workers().record_setup("w1", "succeeded")
    report(traefik=False)
    assert worker_state("w1", "red", frozenset()).view.reason == "traefik_down"


def test_a_worker_is_its_configuration_and_its_decided_state_with_what_it_was_decided_from(db):
  worker = Worker.create(hostname="w1", subdomain="red")
  report = WorkerReport("known", True, ("vector",), (("traefik", "running"), ("vector", "restarting")), 7)
  certificates = {"worker": {"covered": True, "synced": True, "expiry": "2027-01-01", "error": None},
                  "discovery": {"covered": False, "synced": False, "expiry": None, "error": "not issued"}}
  state = WorkerState(WorkerView("not ready", "stack_down", "vector"), "reachable", "succeeded", report,
                      frozenset({"reboot_worker", "add_worker", "remove_worker", "converge_worker"}), "100.64.0.2", "direct",
                      certificates, "healthy")
  payload = worker_payload(worker, state, "sage-example.test-w1", {"cpu_pct": 12.0})
  assert (payload["hostname"], payload["subdomain"], payload["address"], payload["path"]) == ("w1", "red", "100.64.0.2", "direct")
  assert payload["view"] == {"status": "not ready", "reason": "stack_down", "detail": "vector", "ready": False}
  assert (payload["ssh"], payload["setup"], payload["tasks"]) == (
      "reachable", "succeeded", ["add_worker", "converge_worker", "reboot_worker", "remove_worker"])
  assert payload["report"] == {"knowledge": "known", "traefik_reachable": True, "stack_down": ("vector",),
                               "stack": (("traefik", "running"), ("vector", "restarting")), "containers": 7}
  assert (payload["certificates"], payload["tunnel"], payload["tunnel_name"], payload["metrics"]) == (
      certificates, "healthy", "sage-example.test-w1", {"cpu_pct": 12.0})
