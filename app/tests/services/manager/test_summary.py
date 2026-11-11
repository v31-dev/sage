from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from services.containers.decisions import HEADLINES
from services.db.models import Backup, Container, Notification
from services.domains.decisions import STATUSES as DOMAIN_STATUSES
from services.history import record_container_event
from services.manager import summary as module
from services.manager.queue import SCOPES, QueueMixin
from services.manager.summary import SummaryMixin
from services.notification import Notifications
from services.settings import Settings
from services.workers.decisions import STATUSES as WORKER_STATUSES
from utils.queue import TaskQueue


def states(field, *values):
  return {str(index): SimpleNamespace(view=SimpleNamespace(**{field: value})) for index, value in enumerate(values)}


class Summarised(SummaryMixin, QueueMixin):
  """The summary on a real queue; the read models are the states the test sets."""

  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=1)
    self.queue_facts = []
    self.task_queue = TaskQueue(SCOPES, {"common": self.pool}, record=lambda task, status: None,
                                notify=self._queue_changed)
    self.workers, self.containers, self.domains = ("online",), ("active",), ("synced",)

  def worker_states(self):
    return states("status", *self.workers)

  def container_states(self, containers):
    return states("headline", *self.containers)

  def domain_states(self, domains):
    return states("status", *self.domains)


@pytest.fixture
def summarised(db, clock, register, monkeypatch):
  manager = Summarised()
  manager.sent = []
  register(Notifications, SimpleNamespace(dispatch=manager.sent.append))
  monkeypatch.setattr(module, "submit_with_context", lambda executor, func, *args: func(*args))
  yield manager
  manager.pool.shutdown(wait=False, cancel_futures=True)


class TestSummary:
  def test_the_states_are_counted_with_every_one_listed(self, summarised, application):
    summarised.workers, summarised.containers = ("online", "online", "offline"), ("active", "error", "deploying")
    summarised.domains = ("synced", "pending")
    summary = summarised.get_system_summary()
    assert (summary["projects"], summary["applications"]) == (1, 1)
    assert summary["workers"] == {"total": 3, **dict.fromkeys(WORKER_STATUSES, 0), "online": 2, "offline": 1}
    assert summary["containers"] == {"total": 3, **dict.fromkeys(HEADLINES, 0), "active": 1, "error": 1, "deploying": 1}
    assert summary["domains"] == {"total": 2, **dict.fromkeys(DOMAIN_STATUSES, 0), "synced": 1, "pending": 1}

  def test_the_deploys_that_ended_in_the_window_are_counted_and_an_interrupted_one_failed(
          self, summarised, application, worker, clock):
    container = Container.create(application=application, worker=worker)
    record_container_event(container, "deploy", "succeeded")
    clock.advance(timedelta(hours=2))
    for action, transition in [("deploy", "succeeded"), ("deploy", "failed"), ("deploy", "interrupted"),
                               ("deploy", "admitted"), ("deploy", "rejected"), ("stop", "failed")]:
      record_container_event(container, action, transition)
    clock.advance(timedelta(hours=23))
    assert summarised.get_system_summary()["deploys"] == {"succeeded": 1, "failed": 2}

  def test_the_backups_are_counted_by_kind_with_those_of_the_window_and_the_latest(self, summarised, application, clock):
    Backup.create(s3_path="p1", storage_id="s", type="platform")
    clock.advance(timedelta(days=2))
    Backup.create(s3_path="a1", storage_id="s", type="application", application=application)
    Backup.create(s3_path="a2", storage_id="s", type="application", application=application)
    assert summarised.get_system_summary()["backups"] == {"platform": 1, "application": 2, "since": 2,
                                                          "latest_at": clock.current}

  def test_without_backups_there_is_no_latest(self, summarised):
    assert summarised.get_system_summary()["backups"] == {"platform": 0, "application": 0, "since": 0, "latest_at": None}

  def test_the_error_and_warning_notices_of_the_window_are_listed_newest_first(self, summarised, clock):
    Notification.create(content="Old failure.", type="error")
    clock.advance(timedelta(days=2))
    for content, type in [("Deployed.", "success"), ("Failed.", "error"), ("Recovered.", "warning"), ("Stopped.", "info")]:
      Notification.create(content=content, type=type)
      clock.advance(timedelta(minutes=1))
    notices = summarised.get_system_summary()["notices"]
    assert (notices["error"], notices["warning"]) == (1, 1)
    assert [event["content"] for event in notices["events"]] == ["Recovered.", "Failed."]


class TestDailyMessage:
  def test_a_platform_where_all_is_well_is_reported_as_information(self, summarised, application):
    summarised.workers, summarised.containers, summarised.domains = ("online",) * 2, ("active", "inactive"), ("synced",)
    summarised.send_summary_notification()
    [message] = summarised.sent
    assert message["type"] == "info"
    assert message["content"].splitlines() == [
        "Daily summary (2026-01-01 12:00 UTC)",
        "",
        "Workers: 2 of 2 online",
        "Projects: 1, applications: 1",
        "Containers: 2 (1 active, 1 inactive)",
        "Domains: 1 (1 synced)",
        "Deploys in the last 24h: 0 succeeded, 0 failed",
        "Backups: 0 platform, 0 volume archives; 0 in the last 24h",
        "Notices in the last 24h: 0 errors, 0 warnings",
    ]

  def test_containers_under_way_are_counted_together_and_what_is_not_online_is_named(self, summarised):
    summarised.workers = ("online", "not ready", "rebooting")
    summarised.containers = ("active", "deploying", "stopping", "backup", "restoring", "checking")
    summarised.domains = ()
    summarised.send_summary_notification()
    lines = summarised.sent[0]["content"].splitlines()
    assert lines[2] == "Workers: 1 of 3 online (1 rebooting, 1 not ready)"
    assert lines[4:6] == ["Containers: 6 (1 active, 4 in progress, 1 checking)", "Domains: 0"]

  @pytest.mark.parametrize("change, severity", [
      ({"containers": ("active", "error")}, "error"),
      ({"domains": ("synced", "error")}, "error"),
      ({"workers": ("online", "offline")}, "error"),
      ({"workers": ("online", "not ready")}, "warning"),
      ({"workers": ("online", "checking")}, "warning"),
      ({"domains": ("pending",), "containers": ("inactive", "deploying")}, "info"),
  ])
  def test_its_severity_follows_what_is_wrong_now(self, summarised, change, severity):
    for field, values in change.items():
      setattr(summarised, field, values)
    summarised.send_summary_notification()
    assert summarised.sent[0]["type"] == severity

  @pytest.mark.parametrize("notices, severity", [(("warning",), "warning"), (("warning", "error"), "error")])
  def test_its_severity_follows_the_notices_of_the_day(self, summarised, notices, severity):
    for type in notices:
      Notification.create(content="Something.", type=type)
    summarised.send_summary_notification()
    assert summarised.sent[0]["type"] == severity

  def test_it_is_queued_only_with_a_channel_to_send_it_to(self, summarised):
    assert summarised.request_summary() is None
    Settings().set("notifications", {"discord_webhook": "https://discord.example.test/api/webhooks/1/x"})
    summarised.request_summary()
    [task] = summarised.task_queue.snapshot()["queued"]
    assert (tuple(task["key"]), task["scopes"], task["executor"], task["quiet"]) == (
        ("send_summary_notification",), [], "common", False)
