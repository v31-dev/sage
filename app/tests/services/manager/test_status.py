import asyncio
from datetime import datetime, timedelta

import pytest

import config
from services.db import Application, Container, Worker
from services.manager.application import ApplicationMixin
from services.manager.decisions import ABSENT, OFFLINE, SEEN, UNKNOWN, Observation
from services.reports import ContainerReading, HostReading, Reports, Snapshot
from utils.queue import OnConflict

STAMP = "2026-01-01T11:00:00.000000"


class FakeTailscale:
  """The commands the manager runs on a worker, and whether stopping a container fails."""

  def __init__(self):
    self.commands = []
    self.stop_error = None

  async def exec_command(self, hostname, command, timeout=300):
    self.commands.append(command)
    if " down" in command and self.stop_error:
      raise self.stop_error
    return 0, []


class Harness(ApplicationMixin):
  worker_home_dir = "/opt/sage"

  def __init__(self):
    self.tailscale = FakeTailscale()
    self.notices = []
    self.traefik_syncs = []
    self.tasks = []

  def notify(self, message, level="info"):
    self.notices.append((level, message))

  def request_application_traefik_sync(self, application):
    self.traefik_syncs.append(application.id)

  def add_task(self, **kwargs):
    self.tasks.append(kwargs)

  def sync(self, application):
    asyncio.run(self.sync_application_status(application.id))


def reading(status="running", deployed_at=STAMP, name="demo-web"):
  return ContainerReading(name=name, status=status, image="img", deployed_at=deployed_at, exit_code=0, oom_killed=False,
                          started_at=1, finished_at=None, health=None, cpu_percent=None, mem_bytes=None,
                          net_rx_bytes=None, net_tx_bytes=None)


def report(*containers):
  return [Snapshot(at=datetime(2026, 1, 1, 12, 0, 30), host=HostReading(*([None] * 13)),
                   containers={c.name: c for c in containers}, traefik_reachable=True)]


@pytest.fixture
def harness(clock):
  return Harness()


@pytest.fixture
def deployed(application, worker, clock):
  """An application with one active container on an online worker, written at the frozen time."""
  worker.online = True
  worker.save()
  application.deployed_at = datetime(2026, 1, 1, 11, 0)
  application.status = "active"
  application.save()
  Container.create(application=application, worker=worker, status="active")
  clock.advance(timedelta(seconds=config.REPORT_SETTLE_SECONDS))  # reports from now on can be trusted
  return application


def statuses(application):
  application = type(application).get_by_id(application.id)
  return application.status, [c.status for c in application.containers]


class TestAReportThatCanBeTrusted:
  def test_it_decides_and_the_worker_is_not_asked(self, harness, deployed):
    Reports().accept("w1", report(reading()))
    harness.sync(deployed)
    assert harness.tailscale.commands == []
    assert statuses(deployed) == ("active", ["active"])

  def test_a_container_it_shows_as_exited_goes_to_error_and_so_does_the_application(self, harness, deployed):
    Reports().accept("w1", report(reading("exited")))
    harness.sync(deployed)
    assert statuses(deployed) == ("error", ["error"])
    assert ("error", "Application container w1-demo-web is in error state (exited).") in harness.notices

  def test_a_container_it_does_not_list_is_not_running(self, harness, deployed):
    Reports().accept("w1", report())
    harness.sync(deployed)
    assert statuses(deployed) == ("error", ["error"])

  def test_a_container_running_again_is_active_again(self, harness, deployed, clock):
    Container.update(status="error").execute()
    deployed.status = "error"
    deployed.save()
    clock.advance(timedelta(seconds=config.REPORT_SETTLE_SECONDS))
    Reports().accept("w1", report(reading()))
    harness.sync(deployed)
    assert statuses(deployed) == ("active", ["active"])

  def test_a_change_of_container_status_asks_for_the_traefik_config_to_be_synced(self, harness, deployed):
    Reports().accept("w1", report(reading("exited")))
    harness.sync(deployed)
    assert harness.traefik_syncs == [deployed.id]

  def test_nothing_changing_asks_for_nothing(self, harness, deployed):
    Reports().accept("w1", report(reading()))
    harness.sync(deployed)
    assert harness.traefik_syncs == [] and harness.notices == []


class TestAReportThatCannotBeTrusted:
  """Nothing is known then, so nothing changes: the next report or the next minute decides."""

  def unchanged(self, harness, deployed):
    harness.sync(deployed)
    assert statuses(deployed) == ("active", ["active"])
    assert harness.tailscale.commands == [] and harness.notices == []

  def test_one_that_arrived_too_soon_after_the_last_write(self, harness, deployed):
    deployed.save()  # a write now, with the report arriving in the same instant
    Reports().accept("w1", report(reading("exited")))
    self.unchanged(harness, deployed)

  def test_a_write_to_a_container_row_counts_as_the_last_write_too(self, harness, deployed, clock):
    clock.advance(timedelta(seconds=20))
    deployed.containers.get().save()  # the application row was written long enough ago, the container's was not
    Reports().accept("w1", report(reading("exited")))
    self.unchanged(harness, deployed)

  def test_one_that_is_stale(self, harness, deployed, clock):
    Reports().accept("w1", report(reading("exited")))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    self.unchanged(harness, deployed)

  def test_no_report_at_all(self, harness, deployed):
    self.unchanged(harness, deployed)

  def test_a_report_that_settles_decides_at_the_next_sync(self, harness, deployed, clock):
    deployed.save()
    Reports().accept("w1", report(reading("exited")))
    harness.sync(deployed)
    assert statuses(deployed) == ("active", ["active"])
    clock.advance(timedelta(seconds=config.REPORT_SETTLE_SECONDS))
    Reports().accept("w1", report(reading("exited")))
    harness.sync(deployed)
    assert statuses(deployed) == ("error", ["error"])


class TestAWorkerOutOfThePool:
  def test_its_active_containers_go_to_error_without_asking_it(self, harness, deployed, worker):
    worker.online = False
    worker.save()
    harness.sync(deployed)
    assert harness.tailscale.commands == []
    assert statuses(deployed) == ("error", ["error"])
    assert ("error", "Application container w1-demo-web is in error state (worker offline).") in harness.notices


class TestAStaleVersion:
  def test_it_is_stopped_and_becomes_inactive(self, harness, deployed):
    Reports().accept("w1", report(reading(deployed_at="2025-01-01T00:00:00.000000")))
    harness.sync(deployed)
    assert any(" down" in c and "demo-web" in c for c in harness.tailscale.commands)
    assert statuses(deployed) == ("inactive", ["inactive"])
    assert any("stale version" in message for _, message in harness.notices)

  def test_one_that_cannot_be_stopped_is_an_error(self, harness, deployed):
    harness.tailscale.stop_error = Exception("boom")
    Reports().accept("w1", report(reading(deployed_at="2025-01-01T00:00:00.000000")))
    harness.sync(deployed)
    assert statuses(deployed) == ("error", ["error"])
    assert any("could not be stopped: boom" in message for _, message in harness.notices)


class TestBusyStatuses:
  def test_a_container_left_busy_with_no_operation_is_reset_to_error_and_the_next_report_reconciles_it(
          self, harness, deployed, clock):
    Container.update(status="deploying").execute()
    deployed.status = "deploying"
    deployed.save()
    Reports().accept("w1", report(reading()))
    harness.sync(deployed)  # the reset is a write, which no report so far can be compared with
    assert statuses(deployed) == ("error", ["error"])
    assert any("stuck in 'deploying'" in message for _, message in harness.notices)
    clock.advance(timedelta(seconds=config.REPORT_SETTLE_SECONDS))
    Reports().accept("w1", report(reading()))
    harness.sync(deployed)
    assert statuses(deployed) == ("active", ["active"])

  def test_an_application_with_no_containers_is_inactive(self, harness, application, clock):
    application.status = "active"
    application.save()
    harness.sync(application)
    assert statuses(application) == ("inactive", [])


class TestObserveApplication:
  def observe(self, harness, application, worker, last_write=datetime(2026, 1, 1)):
    return harness.observe_application(application, worker, last_write)

  def test_an_offline_worker(self, harness, deployed, worker):
    worker.online = False
    assert self.observe(harness, deployed, worker) == Observation(OFFLINE)

  def test_the_report_gives_the_state_and_the_version(self, harness, deployed, worker):
    Reports().accept("w1", report(reading("paused", deployed_at="v1")))
    assert self.observe(harness, deployed, worker) == Observation(SEEN, "paused", "v1")

  def test_a_report_without_the_container_is_absent(self, harness, deployed, worker):
    Reports().accept("w1", report(reading(name="another")))
    assert self.observe(harness, deployed, worker) == Observation(ABSENT)

  def test_a_container_with_no_label_has_an_empty_version(self, harness, deployed, worker):
    Reports().accept("w1", report(reading(deployed_at=None)))
    assert self.observe(harness, deployed, worker).deployed_label == ""

  def test_no_report_that_can_be_compared_is_unknown(self, harness, deployed, worker):
    assert self.observe(harness, deployed, worker) == Observation(UNKNOWN)
    Reports().accept("w1", report(reading()))
    just_written = datetime(2026, 1, 1, 12, 0, 10)  # the report arrived at 12:00:15
    assert self.observe(harness, deployed, worker, last_write=just_written) == Observation(UNKNOWN)


class TestRequestingAStatusSync:
  def test_a_request_replaces_a_pending_one_and_is_scoped_to_the_application(self, harness, application):
    harness.request_application_status_sync(application)
    (task,) = harness.tasks
    assert task["task"] == harness.sync_application_status
    assert task["scopes"] == {"app:demo-web"} and task["params"] == {"application_id": application.id}
    assert task["on_conflict"] == OnConflict.REPLACE and task["quiet"] is True

  def test_only_the_applications_with_a_changed_container_on_that_worker_are_requested(self, harness, project, worker):
    web = Application.create(project=project, name="web", label="Web", image="nginx")
    api = Application.create(project=project, name="api", label="Api", image="nginx")
    other = Application.create(project=project, name="other", label="Other", image="nginx")
    elsewhere = Worker.create(hostname="w2", ip="100.64.0.3")
    for application, where in ((web, worker), (api, worker), (other, elsewhere)):
      Container.create(application=application, worker=where)

    harness.request_status_sync_for_containers("w1", {"demo-web", "demo-other", "not-ours"})

    assert [task["params"]["application_id"] for task in harness.tasks] == [web.id]
