import ast
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services.db import Task
from services.manager.queue import SCOPES, QueueMixin
from utils.queue import OnConflict, QueuedTask, TaskQueue


class Harness(QueueMixin):
  pass


def finished(**overrides):
  fields = dict(name="deploy", scopes=frozenset({"platform", "app:demo-web"}), task=lambda: None, params={"application_id": 7},
                executor="app", task_id="t-1")
  return QueuedTask(**{**fields, **overrides})


class TestPersistTask:
  def test_a_finished_task_is_recorded_with_what_identifies_it(self, db):
    Harness()._persist_task(finished(), "failed")
    row = Task.get_by_id("t-1")
    assert (row.name, row.status, row.executor) == ("deploy", "failed", "app")
    assert row.scopes == ["app:demo-web", "platform"]
    assert row.params == {"application_id": 7}
    assert row.finished_at is not None

  def test_a_database_error_does_not_break_the_operation(self, db, monkeypatch, caplog):
    def fails(**fields):
      raise RuntimeError("database is locked")

    monkeypatch.setattr(Task, "create", fails)
    with caplog.at_level(logging.ERROR):
      Harness()._persist_task(finished(), "completed")
    assert any("t-1" in record.getMessage() and "database is locked" in record.getMessage() for record in caplog.records)


@pytest.fixture
async def queue():
  with ThreadPoolExecutor(max_workers=1) as pool:
    queue = TaskQueue(SCOPES, {"app": pool})
    yield queue
    queue.stop()
    running = tuple(queue._tasks)
    for task in running:
      task.cancel()
    await asyncio.gather(*running, return_exceptions=True)


async def hold(domain_id=None):
  await asyncio.Event().wait()


def broad_declarations():
  root = Path(__file__).parents[3]
  declarations = []
  for path in [root / "scheduler.py", *(root / "routes").glob("*.py"), *(root / "services/manager").glob("*.py")]:
    for node in ast.walk(ast.parse(path.read_text())):
      if not isinstance(node, ast.Call):
        continue
      scopes = next((keyword.value for keyword in node.keywords if keyword.arg == "scopes"), None)
      if isinstance(scopes, ast.Set) and all(isinstance(value, ast.Constant) for value in scopes.elts):
        values = {value.value for value in scopes.elts}
        if {"platform", "app"} <= values:
          declarations.append(pytest.param(values, id=f"{path.relative_to(root)}:{node.lineno}"))
  assert len(declarations) >= 12
  return declarations


@pytest.mark.parametrize("scopes", broad_declarations())
@pytest.mark.parametrize("domain_scope", ["domain", "domain:42"])
@pytest.mark.parametrize("broad_first", [True, False])
async def test_every_broad_operation_excludes_parent_and_unlinked_domain_work(queue, scopes, domain_scope, broad_first):
  assert "domain" in scopes
  first, second = (scopes, {domain_scope}) if broad_first else ({domain_scope}, scopes)
  queue.add_task(hold, scopes=first, executor="app", task_id="first")
  queue.dispatch_tick()
  queue.add_task(hold, scopes=second, executor="app", task_id="second")
  queue.dispatch_tick()

  assert [task["task_id"] for task in queue.snapshot()["running"]] == ["first"]
  assert [task["task_id"] for task in queue.snapshot()["queued"]] == ["second"]


class TestDomainScopes:
  @pytest.mark.parametrize("domain_scope", ["domain", "domain:42"])
  async def test_platform_restore_waits_for_running_domain_work(self, queue, domain_scope):
    queue.add_task(hold, scopes={domain_scope}, executor="app", task_id="domain")
    queue.dispatch_tick()
    queue.add_task(hold, scopes={"platform", "app", "domain"}, executor="app", task_id="restore", restore=True)
    queue.dispatch_tick()

    assert queue.state.value == "restoring"
    assert [task["task_id"] for task in queue.snapshot()["running"]] == ["domain"]
    assert [task["task_id"] for task in queue.snapshot()["queued"]] == ["restore"]

  async def test_replacing_one_domains_request_keeps_another_domains_request(self, queue):
    for domain_id, task_id in [(1, "first"), (2, "second"), (1, "latest")]:
      queue.add_task(hold, scopes={f"domain:{domain_id}", "app:demo-web"}, executor="app", task_id=task_id,
                     params={"domain_id": domain_id}, on_conflict=OnConflict.REPLACE)

    assert [task["task_id"] for task in queue.snapshot()["queued"]] == ["second", "latest"]

  async def test_independent_domains_run_together_and_discovery_waits_for_them(self, queue):
    queue.add_task(hold, scopes={"domain:1", "app:demo-web"}, executor="app", task_id="first")
    queue.add_task(hold, scopes={"domain:2", "app:demo-api"}, executor="app", task_id="second")
    queue.dispatch_tick()
    queue.add_task(hold, scopes={"domain"}, executor="app", task_id="discovery")
    queue.dispatch_tick()

    assert {task["task_id"] for task in queue.snapshot()["running"]} == {"first", "second"}
    assert [task["task_id"] for task in queue.snapshot()["queued"]] == ["discovery"]

  async def test_discovery_holds_domain_writes_and_leaves_app_work_independent(self, queue):
    queue.add_task(hold, scopes={"domain"}, executor="app", task_id="discovery")
    queue.dispatch_tick()
    queue.add_task(hold, scopes={"domain:1", "app:demo-web"}, executor="app", task_id="domain")
    queue.add_task(hold, scopes={"app:demo-api"}, executor="app", task_id="deploy")
    queue.dispatch_tick()

    assert {task["task_id"] for task in queue.snapshot()["running"]} == {"discovery", "deploy"}
    assert [task["task_id"] for task in queue.snapshot()["queued"]] == ["domain"]

  async def test_a_domain_waits_for_its_app_while_another_apps_domain_can_run(self, queue):
    queue.add_task(hold, scopes={"app:demo-web"}, executor="app", task_id="deploy")
    queue.dispatch_tick()
    queue.add_task(hold, scopes={"domain:1", "app:demo-web"}, executor="app", task_id="linked")
    queue.add_task(hold, scopes={"domain:2", "app:demo-api"}, executor="app", task_id="other")
    queue.dispatch_tick()

    assert {task["task_id"] for task in queue.snapshot()["running"]} == {"deploy", "other"}
    assert [task["task_id"] for task in queue.snapshot()["queued"]] == ["linked"]
