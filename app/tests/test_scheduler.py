import asyncio
import time
from inspect import iscoroutinefunction

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import scheduler
from tests.routes.fakes import ManagerStandIn

TRIGGERS = sorted({*(fn for fn, _ in scheduler._JOBS), *scheduler._AT_STARTUP}, key=lambda fn: fn.__name__)


@pytest.fixture
def manager(monkeypatch):
  manager = ManagerStandIn()
  monkeypatch.setattr(scheduler, "Manager", lambda: manager)
  return manager


@pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda fn: fn.__name__)
async def test_a_trigger_makes_one_request_of_the_manager_with_arguments_it_takes(manager, trigger):
  await trigger()
  [(name, _, _)] = manager.calls
  assert name.startswith("request_") or name == "dispatch_tick"


@pytest.mark.parametrize("trigger, wanted", [
    (scheduler.start_work, "request_startup"), (scheduler.flush_metrics, "request_metrics_flush"),
    (scheduler.schedule_application_backups, "request_automatic_backups"), (scheduler.cleanup, "request_cleanup")],
    ids=lambda value: getattr(value, "__name__", value))
async def test_the_work_a_trigger_asks_for(manager, trigger, wanted):
  await trigger()
  assert [name for name, _, _ in manager.calls] == [wanted]


async def test_the_automatic_backups_are_asked_for_the_minute_now(manager, clock):
  await scheduler.schedule_application_backups()
  assert manager.called("request_automatic_backups") == [((clock.current,), {})]


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

  async def test_start_registers_every_job_and_requests_the_startup_work_once(self, fresh, manager):
    scheduler.start()
    try:
      scheduled = {fn.__name__ for fn, _ in scheduler._JOBS}
      assert {job.id for job in fresh.get_jobs()} == scheduled | {f"{fn.__name__}_at_startup" for fn in scheduler._AT_STARTUP}
      assert fresh.running
      deadline = time.monotonic() + 5
      while not manager.called("request_startup") and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
      assert [name for name, _, _ in manager.calls if name != "dispatch_tick"] == ["request_startup"]
    finally:
      scheduler.shutdown()
      await asyncio.sleep(0)

  async def test_shutdown_stops_the_scheduler_and_is_harmless_when_it_is_not_running(self, fresh):
    scheduler.shutdown()
    scheduler.start()
    scheduler.shutdown()
    await asyncio.sleep(0)
    assert not fresh.running
