import asyncio
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.encoders import jsonable_encoder

from routes.notifications import list_notifications
from services.db import Notification, Worker
from services.manager.core import CoreMixin
from services.manager.queue import QueueMixin
from services.manager.settings import SettingsMixin
from services.manager.workers import WorkersMixin
from services.notification import Notifications
from services.updates import CHECKS, LATEST_VERSION, NOTIFICATIONS, QUEUE_STATUS, WORKERS, Updates
from utils.executor import active_executor


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


class Queue(QueueMixin):
  """The queue's side of a manager: what the status shows of it, which the tests change."""

  def __init__(self, task_state_delay=0.0):
    self.state = "running"
    self.restoring = None
    self.applying = {"tailscale": None, "cloudflare": None, "s3": None, "notifications": None}
    self.last_queue_status = None
    self._queue_status_lock = threading.Lock()
    self.task_state_delay = task_state_delay

  def task_state(self):
    if self.task_state_delay:
      time.sleep(random.random() * self.task_state_delay)
    return self.state

  def restoring_from(self):
    return self.restoring

  def applying_tasks(self):
    return dict(self.applying)


class TestQueue:
  @pytest.fixture
  def queue(self, changes):
    queue = Queue()
    queue._queue_changed()
    changes.sources.clear()
    return queue

  def test_the_status_is_reported_the_first_time(self, changes):
    Queue()._queue_changed()
    assert changes.sources == [QUEUE_STATUS]

  def test_a_change_that_the_status_does_not_show_is_not_reported_for_it(self, queue, changes):
    queue._queue_changed()
    assert changes.sources == []

  @pytest.mark.parametrize("change", [
      lambda queue: setattr(queue, "state", "paused"),
      lambda queue: setattr(queue, "restoring", "2026-10-28T10:00:00"),
      lambda queue: queue.applying.update(s3={"task_id": "t1"}),
  ], ids=["the state", "the backup being restored", "a section applying"])
  def test_the_status_is_reported_when_something_it_shows_changes(self, queue, changes, change):
    change(queue)
    queue._queue_changed()
    assert changes.sources == [QUEUE_STATUS]
    queue._queue_changed()
    assert changes.sources == [QUEUE_STATUS]

  def test_a_section_that_applies_for_another_task_is_reported(self, queue, changes):
    queue.applying["s3"] = {"task_id": "t1"}
    queue._queue_changed()
    queue.applying["s3"] = {"task_id": "t2"}
    queue._queue_changed()
    assert changes.sources.count(QUEUE_STATUS) == 2

  def test_a_section_that_stops_applying_is_reported(self, queue, changes):
    queue.applying["s3"] = {"task_id": "t1"}
    queue._queue_changed()
    queue.applying["s3"] = None
    queue._queue_changed()
    assert changes.sources.count(QUEUE_STATUS) == 2

  @pytest.mark.parametrize("seed", range(6))
  def test_after_changes_from_many_threads_the_last_status_is_the_current_one(self, changes, seed):
    rng = random.Random(seed)
    queue = Queue(task_state_delay=0.002)

    def run(thread_seed):
      thread_rng = random.Random(thread_seed)
      for _ in range(40):
        queue.state = thread_rng.choice(["running", "paused", "restoring", "stopping"])
        queue.applying["s3"] = thread_rng.choice([None, {"task_id": "t1"}, {"task_id": "t2"}])
        queue._queue_changed()

    threads = [threading.Thread(target=run, args=(rng.random(),)) for _ in range(6)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()

    assert queue.last_queue_status == queue.queue_status()


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
    assert (feed, stored) == (NOTIFICATIONS, 1)
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

  def test_a_new_version_is_reported(self, changes):
    core = self.Core({"tag_name": "v1.2.3", "name": "1.2.3", "body": "notes"})
    core.refresh_latest_release()
    assert changes.sources == [LATEST_VERSION]
    assert core.latest_release["version"] == "1.2.3"

  def test_the_same_version_fetched_again_is_not_reported(self, changes):
    core = self.Core({"tag_name": "v1.2.3", "name": "1.2.3", "body": "notes"})
    core.refresh_latest_release()
    core.refresh_latest_release()
    assert changes.sources == [LATEST_VERSION]

  def test_the_version_that_is_already_known_is_not_reported(self, changes):
    core = self.Core({"tag_name": "v1.0.0", "name": "1.0.0", "body": "notes"})
    core.refresh_latest_release()
    assert changes.sources == []

  def test_a_release_that_could_not_be_fetched_is_not(self, changes):
    core = self.Core(RuntimeError("offline"))
    core.refresh_latest_release()
    assert changes.sources == []


class TestWorkers:
  class Workers(WorkersMixin):
    worker_summary = None

  def test_the_worker_counts_are_reported_when_they_are_recorded(self, db, changes):
    Worker.create(hostname="w1", ip="100.64.0.2", online=True)
    Worker.create(hostname="w2", ip="100.64.0.3", online=False)
    workers = self.Workers()
    workers.refresh_worker_summary()
    assert workers.worker_summary == {"total": 2, "online": 1}
    assert changes.sources == [WORKERS]

  def test_the_same_counts_recorded_again_are_not_reported(self, db, changes):
    Worker.create(hostname="w1", ip="100.64.0.2", online=True)
    workers = self.Workers()
    workers.refresh_worker_summary()
    workers.refresh_worker_summary()
    assert changes.sources == [WORKERS]

  def test_counts_that_changed_are_reported(self, db, changes):
    worker = Worker.create(hostname="w1", ip="100.64.0.2", online=True)
    workers = self.Workers()
    workers.refresh_worker_summary()
    worker.online = False
    worker.save()
    workers.refresh_worker_summary()
    assert changes.sources == [WORKERS, WORKERS]


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
    assert changes.sources == [CHECKS]

  def test_a_check_that_failed_is_reported_too(self, db, changes):
    settings = self.Settings(ValueError("no route"))
    self.run(settings)
    assert settings.setting_checks["tailscale"]["state"] == "error"
    assert changes.sources == [CHECKS]
