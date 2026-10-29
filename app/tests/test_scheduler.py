import asyncio
import inspect
from inspect import iscoroutinefunction
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import scheduler
from services.logs import Logs
from services.manager import Manager
from services.metrics import Metrics
from utils.executor import LANES
from utils.queue import TaskQueue

TRIGGERS = sorted({*(fn for fn, _ in scheduler._JOBS), *scheduler._AT_STARTUP}, key=lambda fn: fn.__name__)
# These two add through other methods of the Manager, not through add_task.
NO_DIRECT_TASK = {"dispatch_tick", "sync_application_status"}


class Service:
  """The methods of a real class, by name and with its signatures. A trigger only passes them to the queue,
  so calling one is an error."""

  def __init__(self, cls):
    self._cls = cls

  def __getattr__(self, name):
    real = getattr(self._cls, name)
    if not callable(real):
      raise AttributeError(name)
    signature = inspect.signature(real)

    def refuse(*args, **kwargs):
      raise AssertionError(f"{self._cls.__name__}.{name} was called; a trigger must only enqueue it")

    async def refuse_async(*args, **kwargs):
      refuse()

    method = refuse_async if iscoroutinefunction(real) else refuse
    method.__name__ = name
    method.__signature__ = signature.replace(parameters=list(signature.parameters.values())[1:])
    return method


class FakeManager(Service):
  """The Manager as the triggers use it: the calls they make are answered, everything else is a `Service` method.
  Every task is added to a real queue, which checks its scopes, its lane and its arguments."""

  def __init__(self, s3_configured=True):
    super().__init__(Manager)
    self.queue = TaskQueue(frozenset(LANES), LANES)
    self.s3 = SimpleNamespace(s3_config={"bucket": "b"} if s3_configured else None)
    self.configured = True
    self.application = SimpleNamespace(id=1, qualified_name="p-a")
    self.status_syncs = []
    self.ticks = 0

  def add_task(self, **params):
    inspect.signature(params["task"]).bind(**(params.get("params") or {}))
    return self.queue.add_task(**params)

  def section_configured(self, key):
    return self.configured

  def get_due_volume_backups(self, now):
    return [(self.application, [SimpleNamespace(id=2)])]

  def request_application_status_sync(self, application):
    self.status_syncs.append(application)

  def dispatch_tick(self):
    self.ticks += 1

  def tasks(self):
    return self.queue.snapshot()["queued"]


def use_fakes(monkeypatch, manager):
  monkeypatch.setattr(scheduler, "Manager", lambda: manager)
  monkeypatch.setattr(scheduler, "Metrics", lambda: Service(Metrics))
  monkeypatch.setattr(scheduler, "Logs", lambda: Service(Logs))
  select = SimpleNamespace(join=lambda *_: [manager.application])
  monkeypatch.setattr(scheduler, "Application", SimpleNamespace(select=lambda *_: select))
  return manager


@pytest.fixture
def manager(monkeypatch):
  return use_fakes(monkeypatch, FakeManager())


@pytest.mark.parametrize("name", ["add_task", "section_configured", "get_due_volume_backups",
                                  "request_application_status_sync", "dispatch_tick"])
def test_the_fake_manager_answers_with_the_real_signatures(name):
  fake = inspect.signature(getattr(FakeManager, name)).parameters.keys()
  assert fake == inspect.signature(getattr(Manager, name)).parameters.keys()


@pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda fn: fn.__name__)
async def test_a_trigger_adds_only_tasks_the_queue_and_the_task_accept(manager, trigger):
  await trigger()
  if trigger.__name__ not in NO_DIRECT_TASK:
    assert manager.tasks()
    assert all(isinstance(task["name"], str) and task["name"] for task in manager.tasks())


async def test_a_setting_that_is_not_configured_is_not_checked(manager):
  manager.configured = False
  await scheduler.check_settings()
  assert manager.tasks() == []


async def test_the_application_status_sync_is_asked_for_every_application(manager):
  await scheduler.sync_application_status()
  assert manager.status_syncs == [manager.application]


async def test_the_dispatch_tick_reaches_the_queue(manager):
  await scheduler.dispatch_tick()
  assert manager.ticks == 1


@pytest.mark.parametrize("trigger", [scheduler.schedule_application_backups, scheduler.backup_database,
                                     scheduler.discover_backups], ids=lambda fn: fn.__name__)
async def test_the_triggers_that_need_s3_add_nothing_without_it(monkeypatch, trigger):
  manager = use_fakes(monkeypatch, FakeManager(s3_configured=False))
  await trigger()
  assert manager.tasks() == []


async def test_the_minutely_metrics_job_hands_the_workers_newest_minutes_to_the_worker_list(manager):
  await scheduler.collect_metrics()
  assert [task["name"] for task in manager.tasks()] == ["collect_self", "flush_worker_metrics"]


async def test_a_task_is_named_after_the_method_it_runs(manager):
  await scheduler.send_summary_notification()
  await scheduler.refresh_latest_release()
  await scheduler.reconcile_traefik_configs()
  assert [task["name"] for task in manager.tasks()] == ["send_summary_notification", "refresh_latest_release",
                                                        "reconcile_traefik_configs"]


class TestRegistration:
  def test_every_job_has_its_own_id(self):
    names = [fn.__name__ for fn, _ in scheduler._JOBS]
    assert len(names) == len(set(names))

  @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda fn: fn.__name__)
  def test_a_trigger_is_a_coroutine_function_of_the_module(self, trigger):
    assert iscoroutinefunction(trigger)
    assert getattr(scheduler, trigger.__name__) is trigger


class TestRunning:
  @pytest.fixture
  def fresh(self, monkeypatch):
    fresh = AsyncIOScheduler()
    monkeypatch.setattr(scheduler, "_scheduler", fresh)
    return fresh

  async def test_start_registers_every_job_and_runs_the_startup_ones_once(self, fresh, manager):
    scheduler.start()
    try:
      scheduled = {fn.__name__ for fn, _ in scheduler._JOBS}
      assert {job.id for job in fresh.get_jobs()} == scheduled | {f"{fn.__name__}_at_startup" for fn in scheduler._AT_STARTUP}
      assert fresh.running
      await asyncio.sleep(0.1)
      names = [task["name"] for task in manager.tasks()]
      assert {"refresh_latest_release", "renew_certificates", "discover_s3_backups", "sync_workers",
              "refresh_worker_paths"} <= set(names)
      assert "backup_database" not in names, "a job that has not come due ran at startup"
      assert len(names) == len(set(names))
    finally:
      scheduler.shutdown()
      await asyncio.sleep(0)

  async def test_shutdown_stops_the_scheduler_and_is_harmless_when_it_is_not_running(self, fresh):
    scheduler.shutdown()
    scheduler.start()
    scheduler.shutdown()
    await asyncio.sleep(0)
    assert not fresh.running
