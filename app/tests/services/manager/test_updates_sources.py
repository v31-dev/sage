import ast
import asyncio
import inspect
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.encoders import jsonable_encoder

import config
from routes.notifications import list_notifications
from services.db import Application, Backup, Container, Notification, Task, Worker
from services.manager import Manager
from services.manager.application import application_changed, container_changed, hosts_changed, save_application, save_container
from services.manager.core import CoreMixin
from services.manager.decisions import WORKER_TASKS
from services.manager.platform import PlatformMixin
from services.manager.queue import QueueMixin, task_history
from services.manager.settings import SettingsMixin
from services.manager.traefik import TraefikMixin
from services.manager.workers import WorkersMixin
from services.notification import Notifications
from services.updates import Fact, UpdateFeed, Updates, UpdateSource
from services.worker_reports import WorkerReports
from utils.common import utcnow
from utils.executor import active_executor
from utils.queue import QueuedTask


class Changes:
  def __init__(self):
    self.sources = []
    self.published = []

  def changed(self, source):
    self.sources.append(source)

  def publish(self, feed, rows):
    self.published.append((feed, rows))


@pytest.fixture
def changes(register):
  return register(Updates, Changes())


class TestQueueFacts:
  class Queue(QueueMixin):
    """What the queue holds that topics show, which the tests change."""

    def __init__(self):
      self.snapshot = {"running": [], "queued": []}
      self.state = "running"
      self.queue_facts = self.tasks_queue_facts()

    def task_snapshot(self):
      return {name: list(tasks) for name, tasks in self.snapshot.items()}

    def task_state(self):
      return self.state

  @pytest.fixture
  def queue(self, changes):
    queue = self.Queue()
    queue._queue_changed()
    changes.sources.clear()
    return queue

  def test_the_queue_declares_its_tasks_and_its_state(self):
    queue = self.Queue()
    queue.state = "paused"
    queue.snapshot["running"].append({"task_id": "t1"})
    values = {fact._source: fact._compute() for fact in queue.tasks_queue_facts()}
    assert values == {UpdateSource.TASKS: {"running": [{"task_id": "t1"}], "queued": []},
                      UpdateSource.QUEUE_STATE: "paused"}

  def test_both_are_reported_the_first_time(self, changes):
    self.Queue()._queue_changed()
    assert sorted(changes.sources) == [UpdateSource.QUEUE_STATE, UpdateSource.TASKS]

  def test_a_change_that_nothing_shows_is_not_reported(self, queue, changes):
    queue._queue_changed()
    assert changes.sources == []

  def test_a_task_that_starts_and_ends_is_reported_for_the_tasks_only(self, queue, changes):
    queue.snapshot["running"].append({"task_id": "t1", "name": "deploy"})
    queue._queue_changed()
    queue.snapshot["running"].clear()
    queue._queue_changed()
    assert changes.sources == [UpdateSource.TASKS, UpdateSource.TASKS]

  def test_the_state_changing_is_reported_for_the_state_only(self, queue, changes):
    queue.state = "paused"
    queue._queue_changed()
    assert changes.sources == [UpdateSource.QUEUE_STATE]

  def test_every_fact_of_every_owner_is_checked(self, queue, changes):
    seen = []
    queue.queue_facts.append(Fact(lambda: seen.append("checked") or len(seen), UpdateSource.RESTORING))
    queue._queue_changed()
    assert seen == ["checked"]
    assert changes.sources == [UpdateSource.RESTORING]


class TestSettingsQueueFacts:
  class Settings(SettingsMixin):
    def __init__(self):
      self.applying = {"tailscale": None, "cloudflare": None, "s3": None, "notifications": None}

    def applying_tasks(self):
      return dict(self.applying)

  def test_the_task_of_each_section_is_what_applies_it(self):
    settings = self.Settings()
    settings.applying["s3"] = {"task_id": "t1"}
    assert settings.applying_task_ids() == (None, None, "t1", None)

  def test_a_section_starting_to_apply_and_stopping_is_reported(self, changes):
    settings = self.Settings()
    [fact] = settings.settings_queue_facts()
    fact.check()
    changes.sources.clear()
    settings.applying["s3"] = {"task_id": "t1"}
    fact.check()
    settings.applying["s3"] = None
    fact.check()
    assert changes.sources == [UpdateSource.SETTINGS_APPLYING, UpdateSource.SETTINGS_APPLYING]

  def test_another_task_for_the_same_section_is_reported(self, changes):
    settings = self.Settings()
    [fact] = settings.settings_queue_facts()
    settings.applying["s3"] = {"task_id": "t1"}
    fact.check()
    changes.sources.clear()
    settings.applying["s3"] = {"task_id": "t2"}
    fact.check()
    assert changes.sources == [UpdateSource.SETTINGS_APPLYING]

  def test_nothing_is_reported_when_it_stays_as_it_was(self, changes):
    settings = self.Settings()
    [fact] = settings.settings_queue_facts()
    fact.check()
    changes.sources.clear()
    fact.check()
    assert changes.sources == []


class TestPlatformQueueFacts:
  class Platform(PlatformMixin):
    def __init__(self):
      self.restoring = None
      self.backup = {"platform_backup_in_progress": False, "discovery_in_progress": False}

    def restoring_from(self):
      return self.restoring

    def backup_status(self):
      return dict(self.backup)

  @pytest.fixture
  def facts(self, changes):
    platform = self.Platform()
    facts = platform.platform_queue_facts()
    for fact in facts:
      fact.check()
    changes.sources.clear()
    return platform, facts

  def test_the_restore_and_the_backups_are_declared(self):
    assert [fact._source for fact in self.Platform().platform_queue_facts()] == [UpdateSource.RESTORING,
                                                                                 UpdateSource.BACKUP_STATUS]

  def test_a_restore_starting_is_reported_for_the_restore_only(self, facts, changes):
    platform, checks = facts
    platform.restoring = "2026-10-28T10:00:00"
    for fact in checks:
      fact.check()
    assert changes.sources == [UpdateSource.RESTORING]

  def test_a_backup_or_a_discovery_starting_is_reported_for_the_backups_only(self, facts, changes):
    platform, checks = facts
    platform.backup["platform_backup_in_progress"] = True
    for fact in checks:
      fact.check()
    platform.backup["discovery_in_progress"] = True
    for fact in checks:
      fact.check()
    assert changes.sources == [UpdateSource.BACKUP_STATUS, UpdateSource.BACKUP_STATUS]


class TestBackupStatus:
  class Platform(PlatformMixin):
    def __init__(self, running=(), queued=()):
      self.running = set(running)
      self.queued = set(queued)

    def is_task_running(self, task, scopes=None):
      return task.__name__ in self.running

    def has_task(self, task, params=None):
      return task.__name__ in self.queued

  def test_neither_running_nor_discovering(self):
    assert self.Platform().backup_status() == {"platform_backup_in_progress": False, "discovery_in_progress": False}

  def test_each_flag_follows_its_own_task(self):
    status = self.Platform(running=["backup_database_s3"]).backup_status()
    assert status == {"platform_backup_in_progress": True, "discovery_in_progress": False}
    status = self.Platform(queued=["discover_s3_backups"]).backup_status()
    assert status == {"platform_backup_in_progress": False, "discovery_in_progress": True}


class TestTaskHistory:
  @staticmethod
  def task(**overrides):
    fields = dict(name="deploy", scopes=frozenset({"platform", "app:demo-web"}), task=lambda: None,
                  params={"application_id": 7}, executor="app", task_id="t-1")
    return QueuedTask(**{**fields, **overrides})

  def test_a_finished_task_is_published_once_it_is_stored(self, db, changes):
    seen = []
    changes.publish = lambda feed, rows: seen.append((feed, rows, Task.select().count()))
    QueueMixin()._persist_task(self.task(), "failed")
    [(feed, rows, stored)] = seen
    assert (feed, stored) == (UpdateFeed.TASK_HISTORY, 1)
    assert len(rows) == 1 and (rows[0]["task_id"], rows[0]["status"]) == ("t-1", "failed")

  def test_the_published_row_is_the_row_the_history_returns(self, db, changes):
    QueueMixin()._persist_task(self.task(), "completed")
    [(_, rows)] = changes.published
    assert jsonable_encoder(rows) == jsonable_encoder(task_history()["tasks"])

  def test_the_row_keeps_when_the_task_was_queued_started_and_finished(self, db, changes, clock):
    clock.advance(timedelta(minutes=3))
    QueueMixin()._persist_task(self.task(queued_at=datetime(2026, 1, 1, 12, 0), started_at=datetime(2026, 1, 1, 12, 1)),
                               "completed")

    row = Task.get_by_id("t-1")
    assert (row.created_at, row.started_at, row.finished_at) == (
        datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 12, 1), datetime(2026, 1, 1, 12, 3))

  def test_a_task_that_could_not_be_stored_is_not_published(self, db, changes, monkeypatch):
    def fails(**fields):
      raise RuntimeError("database is locked")

    monkeypatch.setattr(Task, "create", fails)
    QueueMixin()._persist_task(self.task(), "completed")
    assert changes.published == []

  def test_a_task_that_is_finished_is_not_a_change_of_a_source(self, db, changes):
    QueueMixin()._persist_task(self.task(), "completed")
    assert changes.sources == []


class TestNotifications:
  @pytest.fixture
  def core(self, db, register):
    register(Notifications, SimpleNamespace(dispatch=lambda notification: None))
    return CoreMixin()

  def test_a_notification_is_published_to_its_feed_once_it_is_stored(self, core, changes):
    seen = []
    changes.publish = lambda feed, rows: seen.append((feed, rows, Notification.select().count()))
    core.notify("deployed", "success")
    [(feed, rows, stored)] = seen
    assert (feed, stored) == (UpdateFeed.NOTIFICATIONS, 1)
    assert len(rows) == 1 and rows[0]["content"] == "deployed"

  def test_the_published_row_is_the_row_the_list_returns(self, core, changes):
    core.notify("deployed", "success", "https://example.test/apps/1")
    [(_, rows)] = changes.published
    assert jsonable_encoder(rows) == jsonable_encoder(list_notifications())

  def test_a_notification_is_not_a_change_of_a_source(self, core, changes):
    core.notify("deployed")
    assert changes.sources == []


class TestLatestVersion:
  class Core(CoreMixin):
    latest_release = None
    latest_version = "1.0.0"

    def __init__(self, release):
      self.release = release

    def _fetch_latest_release(self):
      if isinstance(self.release, Exception):
        raise self.release
      return self.release

  def test_a_fetched_release_is_reported_and_a_new_version_too(self, changes):
    core = self.Core({"tag_name": "v1.2.3", "name": "1.2.3", "body": "notes"})
    core.refresh_latest_release()
    assert changes.sources == [UpdateSource.RELEASE, UpdateSource.LATEST_VERSION]
    assert core.latest_release["version"] == "1.2.3"

  def test_the_same_version_fetched_again_is_a_release_and_not_a_new_version(self, changes):
    core = self.Core({"tag_name": "v1.2.3", "name": "1.2.3", "body": "notes"})
    core.refresh_latest_release()
    core.refresh_latest_release()
    assert changes.sources == [UpdateSource.RELEASE, UpdateSource.LATEST_VERSION, UpdateSource.RELEASE]

  def test_the_version_that_is_already_known_is_only_a_release(self, changes):
    core = self.Core({"tag_name": "v1.0.0", "name": "1.0.0", "body": "notes"})
    core.refresh_latest_release()
    assert changes.sources == [UpdateSource.RELEASE]

  def test_a_release_that_could_not_be_fetched_is_not(self, changes):
    core = self.Core(RuntimeError("offline"))
    core.refresh_latest_release()
    assert changes.sources == []


class TestWorkersQueueFacts:
  class Workers(WorkersMixin):
    def __init__(self):
      self.snapshot = {"running": [], "queued": []}
      self.given = []

    def task_snapshot(self):
      return {name: list(tasks) for name, tasks in self.snapshot.items()}

    def _set_worker_tasks(self, busy):
      self.given.append(busy)

  @staticmethod
  def task(name, hostname="w1", task_id="t1"):
    return {"task_id": task_id, "name": name, "params": {"worker_hostname": hostname}}

  @pytest.fixture
  def workers(self):
    workers = self.Workers()
    [workers.fact] = workers.workers_queue_facts()
    workers.fact.check()
    workers.given.clear()
    return workers

  @pytest.mark.parametrize("name", WORKER_TASKS)
  def test_a_worker_being_added_removed_or_rebooted_is_given_to_the_worker_list(self, workers, name):
    workers.snapshot["queued"].append(self.task(name))
    workers.fact.check()
    assert workers.given == [((name, "w1"),)]

  def test_the_same_task_moving_from_queued_to_running_gives_nothing_new(self, workers):
    workers.snapshot["queued"].append(self.task("reboot_worker"))
    workers.fact.check()
    workers.snapshot["running"].append(workers.snapshot["queued"].pop())
    workers.fact.check()
    assert workers.given == [(("reboot_worker", "w1"),)]

  def test_the_task_ending_gives_the_empty_set(self, workers):
    workers.snapshot["running"].append(self.task("remove_worker"))
    workers.fact.check()
    workers.snapshot["running"].clear()
    workers.fact.check()
    assert workers.given == [(("remove_worker", "w1"),), ()]

  def test_another_task_gives_nothing(self, workers):
    workers.snapshot["running"].append({"task_id": "t1", "name": "deploy_application", "params": {"application_id": 1}})
    workers.fact.check()
    assert workers.given == []

  def test_another_worker_being_rebooted_is_a_change(self, workers):
    workers.snapshot["running"].append(self.task("reboot_worker", "w1", "t1"))
    workers.fact.check()
    workers.snapshot["running"].append(self.task("reboot_worker", "w2", "t2"))
    workers.fact.check()
    assert workers.given[-1] == (("reboot_worker", "w1"), ("reboot_worker", "w2"))

  def test_it_is_no_source(self, workers, changes):
    workers.snapshot["queued"].append(self.task("add_worker"))
    workers.fact.check()
    assert changes.sources == []

  def test_the_busy_workers_are_the_task_and_the_hostname_sorted(self, workers):
    workers.snapshot["running"].append(self.task("reboot_worker", "w2", "t2"))
    workers.snapshot["queued"].append(self.task("add_worker", "w1", "t1"))
    assert workers.worker_tasks_busy() == (("add_worker", "w1"), ("reboot_worker", "w2"))


class TestTheManagerChecksEveryOwner:
  def test_each_owners_queue_facts_are_checked_by_the_manager(self):
    owners = [name for name in dir(Manager) if name.endswith("_queue_facts")]
    assert sorted(owners) == ["platform_queue_facts", "settings_queue_facts", "tasks_queue_facts", "workers_queue_facts"]
    source = inspect.getsource(sys.modules[Manager.__module__])
    assert [name for name in owners if f"self.{name}()" not in source] == []


class TestApplicationAndContainerChanges:
  """The pages of one application and of one worker are told what changed about them, and nothing else."""

  @pytest.fixture
  def keyed(self, register):
    class Keyed:
      def __init__(self):
        self.signals = []

      def changed(self, source, key=None):
        self.signals.append((source, key))
    return register(Updates, Keyed())

  @pytest.fixture
  def hosted(self, db, application, worker):
    other = Worker.create(hostname="w2", ip="100.64.0.3")
    return Container.create(application=application, worker=worker), Container.create(
        application=Application.create(project=application.project, name="api", label="Api"), worker=other)

  def test_a_change_of_an_application_tells_its_page_only(self, keyed, application):
    application_changed(application)
    assert keyed.signals == [(UpdateSource.APPLICATION, "demo/web")]

  def test_a_change_of_a_container_tells_its_applications_page_and_its_workers(self, keyed, hosted):
    container_changed(hosted[1])
    assert keyed.signals == [(UpdateSource.APPLICATION, "demo/api"), (UpdateSource.WORKER_CONTAINERS, "w2")]

  def test_a_change_of_what_the_workers_show_of_an_application_tells_each_worker_it_is_on_once(self, keyed, hosted, application):
    Container.create(application=application, worker=Worker.create(hostname="w3", ip="100.64.0.4"))
    hosts_changed(application)
    assert sorted(keyed.signals, key=str) == sorted([
        (UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.WORKER_CONTAINERS, "w3")], key=str)

  def test_an_application_with_no_containers_tells_only_itself(self, keyed, application):
    hosts_changed(application)
    assert keyed.signals == [(UpdateSource.APPLICATION, "demo/web")]

  def test_saving_an_application_writes_it_and_says_so(self, keyed, application):
    application.status = "deploying"
    save_application(application)
    assert Application.get_by_id(application.id).status == "deploying"
    assert keyed.signals == [(UpdateSource.APPLICATION, "demo/web")]

  def test_saving_a_container_writes_it_and_says_so(self, keyed, hosted):
    container = hosted[0]
    container.status = "active"
    save_container(container)
    assert Container.get_by_id(container.id).status == "active"
    assert keyed.signals == [(UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1")]

  def test_a_container_that_cannot_be_saved_says_nothing(self, keyed, hosted):
    container = hosted[0]
    container.domain_tag = "x_tag"
    with pytest.raises(ValueError):
      save_container(container)
    assert keyed.signals == []

  def test_a_bulk_update_of_the_domain_sync_says_so(self, keyed, application):
    class Traefik(TraefikMixin):
      def add_task(self, **kwargs):
        pass
    Traefik().request_application_traefik_sync(application)
    assert keyed.signals == [(UpdateSource.APPLICATION, "demo/web")]

  def test_every_write_of_an_application_or_a_container_in_the_manager_goes_through_a_function_that_says_so(self):
    """The only `save()` of an application or container is in `save_application` and `save_container`, and the only
    `delete_instance()` is followed by `container_changed`."""
    saves, deletes = {}, {}
    for path in Path(inspect.getsourcefile(Manager)).parent.glob("*.py"):
      lines = path.read_text().splitlines()
      for number, line in enumerate(lines):
        if re.search(r"\.save\(\)", line):
          saves[path.name] = saves.get(path.name, 0) + 1
        if re.search(r"\.delete_instance\(\)", line):
          deletes[path.name] = deletes.get(path.name, 0) + 1
          assert "container_changed(container)" in lines[number + 1]
    assert saves == {"application.py": 2} and deletes == {"application.py": 1}


class TestBackupRows:
  """The backups list is told whenever a backup row is written."""

  class Platform(PlatformMixin):
    s3 = SimpleNamespace(s3_config=None)

  async def test_the_cleanup_that_removes_backups_says_so(self, db, changes):
    Backup.create(type="platform", s3_path="old", created_at=utcnow() - timedelta(days=config.RETENTION_DAYS + 1))
    Backup.create(type="platform", s3_path="new")
    await self.Platform().cleanup()
    assert [backup.s3_path for backup in Backup.select()] == ["new"]
    assert changes.sources == [UpdateSource.BACKUPS]

  async def test_the_cleanup_that_removes_none_says_nothing(self, db, changes):
    Backup.create(type="platform", s3_path="new")
    await self.Platform().cleanup()
    assert changes.sources == []

  def test_every_function_that_writes_a_backup_row_says_so(self):
    """In the manager and the routes, a function with `Backup.create`, `insert_many` or `delete`, or a
    `generic_delete(Backup, ...)`, also fires the `backups` source."""
    write = re.compile(r"Backup\.(create|insert_many|delete)\(|generic_delete\(Backup\b")
    writers = {}
    folders = [Path(inspect.getsourcefile(Manager)).parent, Path(inspect.getsourcefile(Manager)).parents[1].parent / "routes"]
    for path in (path for folder in folders for path in folder.glob("*.py")):
      source = path.read_text()
      for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.col_offset <= 2:
          body = ast.get_source_segment(source, node)
          if write.search(body):
            writers[f"{path.parent.name}/{path.name}:{node.name}"] = "UpdateSource.BACKUPS" in body
    assert writers == {name: True for name in writers}
    assert set(writers) == {"manager/backups.py:_backup_application_container_volume_to_s3",
                            "manager/platform.py:discover_s3_backups",
                            "manager/platform.py:cleanup", "manager/platform.py:backup_database_s3",
                            "routes/backups.py:delete_backup"}


class TestChecks:
  class Settings(SettingsMixin):
    def __init__(self, result):
      self.setting_checks = {}
      self.result = result

    def test_setting(self, key, value, save):
      if isinstance(self.result, Exception):
        raise self.result
      return self.result

  def run(self, settings):
    pool = ThreadPoolExecutor(max_workers=1)
    token = active_executor.set(pool)
    try:
      asyncio.run(settings.check_setting("tailscale"))
    finally:
      active_executor.reset(token)
      pool.shutdown()

  def test_a_check_that_passed_is_reported(self, db, changes):
    settings = self.Settings("connected")
    self.run(settings)
    assert settings.setting_checks["tailscale"]["state"] == "ok"
    assert changes.sources == [UpdateSource.CHECKS]

  def test_a_check_that_failed_is_reported_too(self, db, changes):
    settings = self.Settings(ValueError("no route"))
    self.run(settings)
    assert settings.setting_checks["tailscale"]["state"] == "error"
    assert changes.sources == [UpdateSource.CHECKS]
