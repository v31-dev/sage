import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

import config
from services.certs import Certs
from services.cloudflare import Cloudflare, CloudflareError
from services.containers.placement import runtime_name
from services.db.models import Application, Container, Domain, Project, Worker, WorkerEvent
from services.manager.queue import SCOPES, QueueMixin
from services.manager.workers import WorkersMixin
from services.metrics import Metrics
from services.tailscale import Tailscale
from services.updates import UpdateSource
from services.worker_reports import ContainerReading, HostReading, Snapshot, WorkerReports
from services.workers.decisions import WorkerView
from services.workers.state import Workers
from tests.services.test_cloudflare import ACCOUNT, TUNNELS, Api, fail, ok
from utils.common import utcnow
from utils.executor import active_executor
from utils.queue import TaskQueue


class Tailnet:
  """The workers' observation: `answers` is what each worker's SSH server will be found to be at the next observation,
  `addresses` each worker's address; the paths asked for are kept. With `failure`, the observation finds the answers
  and then raises it."""

  def __init__(self):
    self.answers, self.addresses, self.found, self.pinged, self.failure = {}, {}, {}, [], None

  async def observe_workers(self, hostnames):
    self.found = {hostname: self.answers.get(hostname, "unobserved") for hostname in hostnames}
    if self.failure is not None:
      raise self.failure

  def ssh(self, hostname):
    return self.found.get(hostname, "unobserved")

  def address(self, hostname):
    return self.addresses.get(hostname)

  def ip(self):
    return "100.64.0.1"

  def path(self, address):
    return None

  async def refresh_paths(self, addresses):
    self.pinged.append(sorted(addresses))


class Manager(WorkersMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = self.workers_queue_facts()
    self.worker_state = self.workers_state_fact()
    self.tunnel_state = self.tunnels_fact()
    self.setup_failures, self.stack_repairs = {}, {}
    self.discovered = 0
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool, "common": self.pool}, record=lambda task, status: None,
                                notify=self._queue_changed)

  def request_discovery(self):
    self.discovered += 1

  def container_payloads(self, containers):
    return [{"id": container.id} for container in containers]


@pytest.fixture
def certificates(register):
  """What the certificates give each worker's state, which a test can change."""
  shown = {}
  register(Certs, SimpleNamespace(worker_state=lambda hostname, subdomain, applied, template: dict(shown.get(hostname, {}))))
  return shown


@pytest.fixture
def tailnet(register, clock, certificates):
  return register(Tailscale, Tailnet())


@pytest.fixture
def manager(tailnet):
  manager = Manager()
  yield manager
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def workers(db):
  return [Worker.create(hostname=hostname, subdomain=subdomain) for hostname, subdomain in (("w1", "red"), ("w2", "blue"))]


def ready(hostname):
  """A worker set up and reporting a working stack."""
  Workers().record_setup(hostname, "succeeded")
  readings = {name: ContainerReading(name=name, status="running", image="", labels={}, exit_code=0, oom_killed=False, started_at=1,
                                     finished_at=None, health=None, cpu_percent=None, mem_bytes=None, net_rx_bytes=None,
                                     net_tx_bytes=None) for name in config.WORKER_STACK_SERVICES}
  WorkerReports().accept(hostname, [Snapshot(at=datetime(2026, 1, 1, 9), host=HostReading(*([None] * 13)), containers=readings,
                                             traefik_reachable=True)])


def pages(*hostnames):
  return [(UpdateSource.WORKER_LIST, None), *((UpdateSource.WORKER_ENTRY, hostname) for hostname in hostnames)]


def events():
  return [(event.subject_id, event.action, event.transition, event.reason) for event in WorkerEvent.select().order_by(WorkerEvent.id)]


class TestObservation:
  async def test_the_workers_found_are_shown_with_what_was_found(self, manager, tailnet, workers):
    tailnet.answers = {"w1": "reachable", "w2": "unreachable"}
    ready("w1")
    await manager.observe_workers()
    states = manager.worker_states()
    assert (states["w1"].view.status, states["w2"].view.status) == ("online", "offline")

  async def test_a_worker_whose_ssh_server_stopped_answering_is_set_up_again_when_it_returns(self, manager, tailnet, workers):
    ready("w1")
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    assert Workers().setup("w1") is None
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    assert (manager.worker_states()["w1"].view.status, manager.worker_states()["w1"].view.reason) == ("checking", "unobserved")

  async def test_paths_are_asked_only_of_reachable_workers_with_an_address(self, manager, tailnet, workers):
    tailnet.answers = {"w1": "reachable", "w2": "unreachable"}
    tailnet.addresses = {"w1": "100.64.0.2", "w2": "100.64.0.3"}
    await manager.observe_workers()
    await manager.refresh_worker_paths()
    assert tailnet.pinged == [["100.64.0.2"]]

  @pytest.mark.parametrize("request_name, key", [("request_worker_observation", ["observe_workers"]),
                                                 ("request_worker_paths", ["refresh_worker_paths"]),
                                                 ("request_tunnel_listing", ["list_tunnels"])])
  def test_observation_tasks_are_quiet_and_hold_no_scope(self, manager, request_name, key):
    getattr(manager, request_name)()
    [task] = manager.task_queue.snapshot()["queued"]
    assert (task["key"], task["scopes"], task["quiet"]) == (key, [], True)

  def test_the_tunnel_listing_runs_on_the_low_priority_lane(self, manager):
    manager.request_tunnel_listing()
    [task] = manager.task_queue.snapshot()["queued"]
    assert task["executor"] == "common"


class TestTunnelListing:
  @pytest.fixture
  def account(self, register, clock, manager, workers):
    """The account's tunnels as Cloudflare lists them, with what the pages were told so far already sent."""
    api = Api()
    cloudflare = register(Cloudflare, Cloudflare())
    cloudflare.http.close()
    cloudflare.http = httpx.Client(base_url="https://cloudflare.test", transport=httpx.MockTransport(api.handle))
    cloudflare.zone_id, cloudflare.account_id = "zone1", ACCOUNT
    token = active_executor.set(manager.pool)
    manager.tunnel_state.check()
    manager.worker_state.check()
    yield api
    active_executor.reset(token)
    cloudflare.http.close()

  async def test_a_listing_tells_the_info_topic_and_the_workers_whose_tunnel_changed_once(self, manager, account, signals):
    signals.clear()
    account.on("GET", TUNNELS, ok([{"id": "t1", "name": "sage-example.test-w1", "status": "healthy"}]))
    await manager.list_tunnels()
    assert sorted(signals) == sorted([(UpdateSource.TUNNELS, None), *pages("w1")])
    signals.clear()
    await manager.list_tunnels()
    assert signals == []

  async def test_a_failed_listing_fails_and_tells_the_pages_once_what_was_known_has_grown_old(
          self, manager, account, signals, clock):
    account.on("GET", TUNNELS, ok([{"id": "t1", "name": "sage-example.test-w1", "status": "healthy"}]))
    await manager.list_tunnels()
    account.on("GET", TUNNELS, fail(403, "Authentication error"))
    clock.advance(timedelta(seconds=config.TUNNEL_LISTING_STALE_SECONDS))
    signals.clear()
    with pytest.raises(CloudflareError):
      await manager.list_tunnels()
    assert signals == []
    clock.advance(timedelta(seconds=1))
    with pytest.raises(CloudflareError):
      await manager.list_tunnels()
    assert sorted(signals) == sorted([(UpdateSource.TUNNELS, None), *pages("w1")])


class TestPublication:
  async def test_only_the_workers_whose_state_changed_are_shown_again(self, manager, tailnet, workers, signals):
    tailnet.answers = {"w1": "reachable", "w2": "reachable"}
    ready("w1")
    ready("w2")
    await manager.observe_workers()
    signals.clear()
    await manager.observe_workers()
    assert signals == []
    tailnet.answers["w2"] = "unreachable"
    await manager.observe_workers()
    assert signals == pages("w2")

  async def test_an_observation_that_fails_still_shows_the_workers_whose_state_changed_and_fails(
          self, manager, tailnet, workers, signals):
    tailnet.answers = {"w1": "reachable", "w2": "reachable"}
    await manager.observe_workers()
    signals.clear()
    tailnet.answers, tailnet.failure = {"w1": "unknown", "w2": "unknown"}, Exception("Tailscale status timed out")
    with pytest.raises(Exception, match="timed out"):
      await manager.observe_workers()
    assert sorted(signals) == sorted(pages("w1", "w2"))

  async def test_a_worker_whose_readiness_changed_shows_the_domains_routed_to_it_again(
          self, manager, tailnet, workers, application, signals):
    Domain.create(name="shop", type="public", port=80, container=Container.create(application=application, worker="w2"))
    Domain.create(name="api", type="internal", port=80, namespace_worker="w2")
    Domain.create(name="other", type="internal", port=80, container=Container.create(application=application, worker="w1"),
                  namespace_worker="w1")
    tailnet.answers = {"w1": "reachable", "w2": "reachable"}
    ready("w1")
    ready("w2")
    await manager.observe_workers()
    signals.clear()
    tailnet.answers["w2"] = "unreachable"
    await manager.observe_workers()
    assert signals[:2] == pages("w2")
    assert sorted(signals[2:]) == sorted([(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                                          (UpdateSource.WORKER_CONTAINERS, "w2"), (UpdateSource.DOMAINS, None),
                                          (UpdateSource.WORKER_CONTAINERS, "w2")])

  async def test_a_change_of_its_certificates_shows_its_worker_again(self, manager, tailnet, workers, signals, certificates):
    await manager.observe_workers()
    signals.clear()
    certificates["w1"] = {"worker": {"covered": True, "synced": True}}
    manager.worker_state.check()
    assert signals == pages("w1")

  async def test_a_report_shows_its_worker_again_when_its_state_changed(self, manager, tailnet, workers, signals):
    tailnet.answers = {"w1": "reachable"}
    Workers().record_setup("w1", "succeeded")
    await manager.observe_workers()
    signals.clear()
    ready("w1")
    manager.worker_reported("w1")
    assert signals == pages("w1")

  def test_a_worker_task_counts_for_the_worker_its_key_names_whatever_its_params_say(self, manager, tailnet, workers):
    async def reboot_worker(hostname):
      pass

    manager.add_task(task=reboot_worker, key=("reboot_worker", "w1"), scopes={"worker:w1"}, executor="app", params={"hostname": "w2"})
    states = manager.worker_states()
    assert (states["w1"].view.status, states["w2"].tasks) == ("rebooting", frozenset())

  async def test_a_worker_task_queued_and_ended_shows_its_worker(self, manager, tailnet, workers, signals):
    release = asyncio.Event()

    async def reboot_worker(hostname):
      await release.wait()

    manager.add_task(task=reboot_worker, key=("reboot_worker", "w1"), scopes={"worker:w1"}, executor="app", params={"hostname": "w1"})
    assert signals == pages("w1")
    assert manager.worker_states()["w1"].view.status == "rebooting"
    manager.task_queue.dispatch_tick()
    release.set()
    while manager.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
    assert signals == pages("w1") * 2


class TestDiscoveryRequests:
  async def test_a_worker_becoming_reachable_and_set_up_ready_or_unreachable_republishes_discovery(self, manager, tailnet, workers):
    tailnet.answers, tailnet.addresses = {"w1": "reachable"}, {"w1": "100.64.0.2"}
    await manager.observe_workers()
    assert manager.discovered == 0
    Workers().record_setup("w1", "succeeded")
    manager.worker_reported("w1")
    ready("w1")
    manager.worker_reported("w1")
    tailnet.answers["w1"] = "unreachable"
    await manager.observe_workers()
    assert manager.discovered == 3

  async def test_a_new_address_republishes_it_and_a_check_that_changes_neither_set_does_not(self, manager, tailnet, workers):
    tailnet.answers, tailnet.addresses = {"w1": "reachable"}, {"w1": "100.64.0.2"}
    ready("w1")
    await manager.observe_workers()
    before = manager.discovered
    await manager.observe_workers()
    assert manager.discovered == before
    tailnet.addresses["w1"] = "100.64.0.9"
    await manager.observe_workers()
    assert manager.discovered == before + 1


class TestEvents:
  async def test_observed_changes_between_offline_not_ready_and_online_are_recorded_once_each(
          self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    await manager.observe_workers()
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "reachable"}
    Workers().record_setup("w1", "failed")
    await manager.observe_workers()
    assert [(subject, action, reason) for subject, action, _, reason in events()] == [
        ("w1", "connectivity", {"status": "offline", "reason": "ssh_unreachable"}),
        ("w1", "connectivity", {"status": "not ready", "reason": "setup_failed"})]
    assert {transition for _, _, transition, _ in events()} == {"observed"}
    assert notices() == [("error", "Worker w1 is offline.", "https://manager.core.example.test/workers/w1"),
                         ("success", "Worker w1 is reachable again.", "https://manager.core.example.test/workers/w1")]

  @pytest.mark.parametrize("found", ["unreachable", "unknown"], ids=["not over the tailnet", "manager cut off"])
  async def test_a_worker_whose_tunnel_still_answers_is_not_offline_until_its_tunnel_is_down_too(
          self, manager, tailnet, workers, notices, found):
    cloudflare = Cloudflare()
    cloudflare.tunnel_health, cloudflare.tunnels_listed_at = {"sage-example.test-w1": "healthy"}, utcnow()
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    tailnet.answers = {"w1": found}
    await manager.observe_workers()
    assert notices() == []
    cloudflare.tunnel_health = {"sage-example.test-w1": "down"}
    await manager.observe_workers()
    assert manager.worker_state.last["w1"].view == WorkerView("offline", "unreachable")
    assert notices() == [("error", "Worker w1 is offline.", "https://manager.core.example.test/workers/w1")]

  async def test_a_change_across_checking_is_recorded_from_the_last_status_observed(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    tailnet.answers = {"w1": "unknown"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    assert events() == []
    tailnet.answers = {"w1": "unknown"}
    await manager.observe_workers()
    Workers().record_setup("w1", "failed")
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    assert [(action, reason) for _, action, _, reason in events()] == [
        ("readiness", {"status": "not ready", "reason": "setup_failed"})]
    assert notices() == []

  async def test_a_worker_back_from_offline_is_recorded_online_once_it_is_set_up_again(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    assert manager.worker_state.last["w1"].view.status == "checking"
    ready("w1")
    manager.worker_reported("w1")
    assert [(action, reason["status"]) for _, action, _, reason in events()] == [
        ("connectivity", "offline"), ("connectivity", "online")]

  async def test_a_worker_removed_and_added_again_starts_with_no_status_observed(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    Worker.delete().where(Worker.hostname == "w1").execute()
    manager.worker_state.check()
    Worker.create(hostname="w1", subdomain="red")
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    assert (events(), notices()) == ([], [])

  async def test_a_worker_that_answers_again_says_so_once_while_it_is_set_up_again(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    ready("w1")
    manager.worker_reported("w1")
    assert manager.worker_state.last["w1"].view.status == "online"
    assert [content for _, content, _ in notices()] == ["Worker w1 is offline.", "Worker w1 is reachable again."]

  async def test_a_worker_offline_when_first_observed_or_unknown_to_the_manager_sends_nothing(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "unreachable"}
    await manager.observe_workers()
    tailnet.answers = {"w1": "unknown"}
    await manager.observe_workers()
    assert notices() == []

  async def test_a_worker_first_seen_or_only_checked_records_nothing(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    await manager.observe_workers()
    ready("w1")
    await manager.observe_workers()
    assert events() == [] and notices() == []

  async def test_a_change_without_going_offline_is_recorded_as_readiness(self, manager, tailnet, workers, notices):
    tailnet.answers = {"w1": "reachable"}
    ready("w1")
    await manager.observe_workers()
    Workers().record_setup("w1", "refused")
    manager.worker_reported("w1")
    assert events() == [("w1", "readiness", "observed", {"status": "not ready", "reason": "owned_elsewhere"})]
    assert notices() == []


@pytest.fixture
def metrics(register):
  """What Metrics holds: each worker's newest minute, and each host's graphs."""
  held = SimpleNamespace(newest={}, graphs={})
  register(Metrics, SimpleNamespace(newest=lambda hostname: held.newest.get(hostname),
                                    query_period=lambda hostname, period: held.graphs[hostname]))
  return held


def series(name):
  return [{"ts": "2026-01-01T09:00:00Z", "name": name, "cpu_pct": 1.0}]


class TestPayloads:
  def test_the_list_is_every_configured_worker_by_hostname_with_its_state_tunnel_and_newest_minute(
          self, manager, tailnet, workers, metrics):
    Worker.create(hostname="a0", subdomain="green")
    tailnet.addresses["w2"] = "100.64.0.3"
    metrics.newest["w2"] = {"cpu_pct": 5.0}
    payloads = manager.worker_list()
    assert [(payload["hostname"], payload["address"], payload["metrics"]) for payload in payloads] == [
        ("a0", None, None), ("w1", None, None), ("w2", "100.64.0.3", {"cpu_pct": 5.0})]
    assert payloads[1]["tunnel_name"] == "sage-example.test-w1"
    assert payloads[1]["view"]["status"] == "checking"

  def test_a_workers_page_has_its_containers_by_application(self, manager, workers, metrics, application):
    other = Application.create(project=Project.create(name="alpha", label="Alpha"), name="api", label="Api")
    placed = [Container.create(application=application, worker="w1"), Container.create(application=other, worker="w1")]
    Container.create(application=application, worker="w2")
    page = manager.worker_page("w1")
    assert page["hostname"] == "w1"
    assert page["containers"] == [{"id": placed[1].id}, {"id": placed[0].id}]

  def test_a_workers_page_says_why_it_cant_be_removed_now(self, manager, workers, metrics, application):
    assert manager.worker_page("w1")["removal_refusal"] is None
    domain = Domain.create(name="db", type="internal", port=5432, namespace_worker="w1")
    assert manager.worker_page("w1")["removal_refusal"] == "A worker with internal domains can't be removed."
    domain.delete_instance()
    Container.create(application=application, worker="w1")
    assert manager.worker_page("w1")["removal_refusal"] == "A worker with containers can't be removed."

  def test_a_worker_that_is_not_configured_has_no_page_or_graphs(self, manager, workers, metrics):
    with pytest.raises(LookupError):
      manager.worker_page("nobody")
    with pytest.raises(LookupError):
      manager.worker_graphs("nobody", "1h")

  def test_a_placements_series_is_named_by_its_application_and_a_stack_services_by_its_own_name(
          self, manager, tailnet, workers, metrics, application):
    container = Container.create(application=application, worker="w1")
    gone = "sage-0123456789abcdef"
    tailnet.addresses["w1"] = "100.64.0.2"
    metrics.graphs["w1"] = {"host": [], "meta": {"cpu_cores": 4},
                            "containers": [series(runtime_name(container.id)), series("traefik"), series(gone)]}
    graphs = manager.worker_graphs("w1", "1h")
    assert [(item["name"], item["label"]) for item in graphs["containers"]] == [
        (runtime_name(container.id), "Demo / Web"), ("traefik", "traefik"), (gone, gone)]
    assert graphs["containers"][1]["points"] == series("traefik")
    assert graphs["meta"] == {"cpu_cores": 4, "ip": "100.64.0.2"}

  def test_the_managers_own_graphs_have_its_own_address(self, manager, metrics):
    metrics.graphs["manager"] = {"host": [], "meta": {}, "containers": []}
    assert manager.worker_graphs("manager", "1h")["meta"] == {"ip": "100.64.0.1"}
