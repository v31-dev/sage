import asyncio
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services.applications.deploy_input import capture_deploy_config
from services.containers.placement import placement_dir, record_path, runtime_name
from services.containers.record import Applied, Authority, Operation, Recovery, Settled, TargetRead, TargetRecord, decode_record, encode_record
from services.containers.targets import Containers
from services.db.models import Application, Container, ContainerEvent, Domain, Instance, Volume
from services.logs import Logs
from services.manager.containers import ContainersMixin
from services.manager.lifecycle import LifecycleMixin, mark
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from utils.errors import Conflict
from utils.logging import TaskFailed, task_id
from utils.queue import TaskQueue

# The worker's clock in the tests: 2026-01-01 12:00:00 UTC.
WORKER_CLOCK = 1767268800
VERIFIED = datetime(2026, 1, 1, 12, 0, 1)

DOCKER = """#!/bin/sh
# The worker's Docker: one runtime file per Compose project under $FAKE_DOCKER, and flags there that make it fail. A
# runtime's Docker container is `id-<project>` until it is recreated (`replaced`).
state="$FAKE_DOCKER"
for last; do :; done
case "$1 $2 $4" in
  "compose -p stop")
    [ -e "$state/stop-fails" ] && { echo "stop failed"; exit 1; }
    [ -e "$state/runtime-$3" ] && sed -i 's/^running|/exited|/' "$state/runtime-$3"
    ;;
  "compose -p start")
    [ -e "$state/start-fails" ] && { echo "start failed"; exit 1; }
    [ -e "$state/runtime-$3" ] && sed -i 's/^exited|/running|/' "$state/runtime-$3"
    ;;
  "compose -p ps")
    grep -qs '^running|' "$state/runtime-$3" && { [ -e "$state/replaced" ] && echo "other-$3" || echo "id-$3"; }
    ;;
  "compose -p "*)
    [ -e "$state/down-fails" ] && { echo "down failed"; exit 1; }
    [ -e "$state/down-is-ignored" ] || rm -f "$state/runtime-$3"
    ;;
  "compose -f "*)
    file="$3"
    name=$(sed -n 's/^name: //p' "$file")
    label() { sed -n "s/^ *sage.$1: \\"\\(.*\\)\\"$/\\1/p" "$file"; }
    deployment=$(label deployment)
    [ -e "$state/runs-another" ] && deployment=another
    labels="$deployment|$(label instance)|$(label generation)|$(label container)"
    echo "running|$labels" > "$state/runtime-$name"
    [ -e "$state/up-fails" ] && { echo "pull access denied"; exit 1; }
    [ -e "$state/exits" ] && echo "exited|$labels" > "$state/runtime-$name"
    ;;
  inspect*)
    [ -e "$state/runtime-$last" ] || { echo "Error: No such object: $last" >&2; exit 1; }
    cat "$state/runtime-$last"
    ;;
esac
exit 0
"""


class Worker:
  """A worker whose commands run in a local shell over the test's directory, with Docker and the clock replaced, and
  whose synced files land there, as SSH and SFTP would put them."""

  def __init__(self, root: Path):
    self.state = root / "docker"
    self.state.mkdir()
    binaries = root / "bin"
    binaries.mkdir()
    for name, script in (("docker", DOCKER), ("date", f"#!/bin/sh\necho {WORKER_CLOCK}\n")):
      (binaries / name).write_text(script)
      (binaries / name).chmod(stat.S_IRWXU)
    self.env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "FAKE_DOCKER": str(self.state)}
    self.reachable = True
    self.breaks_at = None
    self.records_fail = False

  def flag(self, *names):
    for name in names:
      (self.state / name).touch()

  def runtime(self, container) -> str | None:
    """The runtime's state and deployment, "running d1"; None without one."""
    path = self.state / f"runtime-{runtime_name(container.id)}"
    return " ".join(path.read_text().strip().split("|")[:2]) if path.exists() else None

  def run(self, container, state, labelled=None):
    """A runtime of the container, "running d1", labelled as `labelled` (this Sage's current authority by default)."""
    authority = labelled or authority_of(container)
    status, deployment = state.split()
    (self.state / f"runtime-{runtime_name(container.id)}").write_text(
        "|".join([status, deployment, authority.instance_id, authority.state_generation, authority.container_id]) + "\n")

  def answer(self, what):
    if self.breaks_at is not None and self.breaks_at in what:
      self.reachable = False
    if not self.reachable:
      raise Exception("Error connecting to w1: no route")

  async def exec_command(self, hostname, command, timeout=300):
    self.answer(command)
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=self.env)
    if result.returncode:
      raise Exception(f"Error executing command on {hostname}: exit code {result.returncode}\n{result.stdout}")
    return result.returncode, result.stdout.splitlines()

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    self.answer(dst)
    if self.records_fail and dst.endswith("record.json"):
      raise Exception(f"Syncing file to {hostname} timed out after {timeout}s: {dst}")
    content = Path(src).read_text()
    for key, value in (keys or {}).items():
      content = content.replace("${" + key + "}", str(value))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(content)
    Path(dst).chmod(mode)


class Lifecycle(ContainersMixin, LifecycleMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = []
    self.recoveries = {}
    self.orphans = {}
    self.applied, self.reconciled = [], 0
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  def request_domain_applies(self, domain_ids):
    if ids := sorted(domain_ids):
      self.applied.append(ids)

  def request_domain_reconciliation(self):
    self.reconciled += 1

  async def run(self):
    self.task_queue.dispatch_tick()
    while self.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
      self.task_queue.dispatch_tick()


@pytest.fixture
def worker_host(register, tmp_path, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", str(tmp_path / "home"))
  return register(Tailscale, Worker(tmp_path))


@pytest.fixture
def manager():
  manager = Lifecycle()
  yield manager
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


@pytest.fixture
def in_task():
  token = task_id.set("t1")
  yield
  task_id.reset(token)


def authority(container, generation=None):
  instance = Instance.get()
  return Authority(instance.id, generation or instance.state_generation, container.id)


authority_of = authority


def worker_record(container) -> TargetRecord | None:
  path = Path(record_path(config.WORKER_HOME_DIR, container.id))
  return decode_record(path.read_text()) if path.exists() else None


def place(container, record: TargetRecord | str):
  path = Path(record_path(config.WORKER_HOME_DIR, container.id))
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(record if isinstance(record, str) else encode_record(record))


def placement_file(container, name) -> Path:
  return Path(placement_dir(config.WORKER_HOME_DIR, container.id)) / name


def events(container):
  return [(event.action, event.transition, event.reason) for event in ContainerEvent.select().order_by(ContainerEvent.id)
          if event.subject_id == container.id]


def deployed(container, at=VERIFIED):
  """A record of a deploy that succeeded."""
  return TargetRecord(authority(container), Applied("d0", "a" * 64, at), Settled("d0", "running", "succeeded", at),
                      Operation("d0", "deploy", "settled", at, 905, at, "a" * 64, outcome="succeeded", verified_on_worker_at=at))


async def deploy(manager, container):
  await manager.deploy_container(container.id, capture_deploy_config(container.application_id))


pytestmark = pytest.mark.usefixtures("thread_connections", "in_task")

MANAGER = "https://manager.core.example.test"
PAGE = f"{MANAGER}/projects/demo/web"


class TestDeploy:
  async def test_a_deploy_commits_what_it_verified_runs(self, manager, worker_host, container, notices):
    await deploy(manager, container)
    assert notices() == [("success", "Demo / Web on w1 deployed.", PAGE)]

    record = worker_record(container)
    operation = record.operation
    assert (record.authority, operation.contract, operation.phase, operation.outcome) == (
        authority(container), "deploy", "settled", "succeeded")
    assert record.applied.deployment_id == operation.id
    assert record.applied.config_hash == capture_deploy_config(container.application_id).content_hash
    assert (record.settled.operation_id, record.settled.runtime_goal, record.settled.result) == (operation.id, "running", "succeeded")
    assert operation.verified_on_worker_at == VERIFIED and operation.effect_bound_seconds == 905
    assert worker_host.runtime(container) == f"running {operation.id}"
    assert Containers().target(container.id) == TargetRead("present", record)
    assert events(container) == [("deploy", "admitted", None), ("deploy", "succeeded", None)]

  async def test_a_deploy_writes_the_env_file_private_and_the_compose_file(self, manager, worker_host, container, application):
    Application.update(env="GREETING=hi").where(Application.id == application.id).execute()
    await deploy(manager, container)
    env = placement_file(container, ".env")
    assert (env.read_text(), stat.S_IMODE(env.stat().st_mode)) == ("GREETING=hi", 0o600)
    assert f'sage.deployment: "{worker_record(container).operation.id}"' in placement_file(container, "docker-compose.yml").read_text()

  async def test_a_redeploy_replaces_the_running_deployment(self, manager, worker_host, container):
    await deploy(manager, container)
    first = worker_record(container).operation.id
    await deploy(manager, container)
    second = worker_record(container).operation.id
    assert first != second and worker_host.runtime(container) == f"running {second}"

  async def test_a_deploy_keeps_its_volumes_and_prunes_the_others_of_sage(self, manager, worker_host, container, application):
    volume = Volume.create(application=application, name="data", path="/data")
    volumes = placement_file(container, "volumes")
    for name in (volume.id, "0123456789abcdef", "planted"):
      (volumes / name).mkdir(parents=True)
    await deploy(manager, container)
    assert sorted(path.name for path in volumes.iterdir()) == sorted([volume.id, "planted"])

  async def test_a_git_deploy_writes_its_build_secrets_private_and_drops_old_ones(
          self, manager, worker_host, container, application):
    Application.update(type="git", image=None, repo="https://github.com/user/api.git", build_secrets="TOKEN=abc").where(
        Application.id == application.id).execute()
    (placement_file(container, "secrets")).mkdir(parents=True)
    placement_file(container, "secrets/OLD").write_text("old")
    await deploy(manager, container)
    secret = placement_file(container, "secrets/TOKEN")
    assert (secret.read_text(), stat.S_IMODE(secret.stat().st_mode)) == ("abc", 0o600)
    assert not placement_file(container, "secrets/OLD").exists()

  async def test_a_deploy_whose_start_fails_leaves_it_stopped_with_the_failure(self, manager, worker_host, container, notices):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    worker_host.flag("up-fails")
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    [(level, content, link)] = notices()
    assert (level, content.startswith("Deploy of Demo / Web on w1 failed: "), "pull access denied" in content, link) == (
        "error", True, True, PAGE)

    record = worker_record(container)
    assert worker_host.runtime(container) is None
    assert record.applied == deployed(container).applied
    assert (record.settled.runtime_goal, record.settled.result) == ("stopped", "failed")
    assert "pull access denied" in record.settled.reason
    assert (record.operation.phase, record.operation.outcome, record.operation.verified_on_worker_at) == (
        "settled", "failed", VERIFIED)
    assert [(action, transition) for action, transition, _ in events(container)] == [("deploy", "admitted"), ("deploy", "failed")]

  @pytest.mark.parametrize("flag, detail", [("runs-another", "runs deployment another"), ("exits", "is exited")])
  async def test_a_deploy_that_does_not_run_its_deployment_has_failed(self, manager, worker_host, container, flag, detail):
    worker_host.flag(flag)
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    record = worker_record(container)
    assert detail in record.settled.reason and record.settled.result == "failed" and record.applied is None
    assert worker_host.runtime(container) is None

  async def test_a_deploy_whose_stop_fails_changes_no_file_and_is_left_recovering(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    worker_host.flag("down-fails")
    with pytest.raises(TaskFailed):
      await deploy(manager, container)

    record = worker_record(container)
    assert not placement_file(container, ".env").exists() and not placement_file(container, "docker-compose.yml").exists()
    assert (record.operation.phase, record.operation.outcome) == ("recovering", "failed")
    assert record.settled == deployed(container).settled
    assert worker_host.runtime(container) == "running d0"

  async def test_a_failed_deploy_that_cannot_be_stopped_is_left_recovering(self, manager, worker_host, container):
    worker_host.flag("up-fails", "down-is-ignored")
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    record = worker_record(container)
    assert (record.operation.phase, record.operation.outcome, record.settled) == ("recovering", "failed", None)
    assert worker_host.runtime(container) == f"running {record.operation.id}"

  async def test_a_worker_lost_during_the_deploy_leaves_its_record_executing_for_recovery(
          self, manager, worker_host, container):
    worker_host.breaks_at = "up -d"
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    record = worker_record(container)
    assert (record.operation.phase, record.operation.outcome) == ("executing", None)
    assert Containers().target(container.id) == TargetRead("present", record)
    assert [(action, transition) for action, transition, _ in events(container)] == [("deploy", "admitted"), ("deploy", "failed")]

  async def test_a_begin_write_that_may_have_failed_counts_as_begun(self, manager, worker_host, container):
    worker_host.run(container, "running old")
    worker_host.records_fail = True
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    assert worker_host.runtime(container) is None
    assert [(action, transition) for action, transition, _ in events(container)] == [("deploy", "admitted"), ("deploy", "failed")]

  async def test_a_deploy_marks_the_application_logs(self, manager, worker_host, container, application):
    await deploy(manager, container)
    assert [line["message"] for line in Logs().query_application_lines("demo-web")["lines"]] == [
        "Deployed (task t1)"]


class TestStop:
  async def test_a_stop_commits_stopped_and_keeps_the_applied_deployment(self, manager, worker_host, container, notices):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    await manager.stop_container(container.id)
    assert notices() == [("success", "Demo / Web on w1 stopped.", PAGE)]

    record = worker_record(container)
    assert worker_host.runtime(container) is None
    assert record.applied == deployed(container).applied
    assert (record.settled.runtime_goal, record.settled.result, record.settled.operation_id) == (
        "stopped", "succeeded", record.operation.id)
    assert (record.operation.contract, record.operation.phase, record.operation.effect_bound_seconds,
            record.operation.verified_on_worker_at) == ("stop", "settled", 65, VERIFIED)
    assert events(container) == [("stop", "admitted", None), ("stop", "succeeded", None)]

  async def test_a_stop_of_a_container_never_deployed_records_it_stopped(self, manager, worker_host, container):
    await manager.stop_container(container.id)
    record = worker_record(container)
    assert (record.applied, record.settled.runtime_goal, record.settled.result) == (None, "stopped", "succeeded")

  async def test_a_stop_that_leaves_it_running_is_left_recovering(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.stop_container(container.id)
    record = worker_record(container)
    assert (record.operation.phase, record.operation.outcome, record.settled) == (
        "recovering", "failed", deployed(container).settled)
    assert "still running" in record.operation.reason


class TestRefusals:
  @pytest.mark.parametrize("operation", ["deploy", "stop"])
  async def test_an_unreachable_worker_refuses_it_with_no_effect(self, manager, worker_host, container, operation):
    worker_host.run(container, "running d0")
    worker_host.reachable = False
    with pytest.raises(TaskFailed):
      await (deploy(manager, container) if operation == "deploy" else manager.stop_container(container.id))
    worker_host.reachable = True
    assert (worker_record(container), worker_host.runtime(container)) == (None, "running d0")
    assert events(container) == [(operation, "rejected", {"refusal": "unreachable"})]

  @pytest.mark.parametrize("found, refusal", [
      ("unsettled", "interrupted"), ("invalid", "invalid_record"), ("another Sage", "authority")])
  async def test_a_record_it_cannot_build_on_refuses_it_untouched(self, manager, worker_host, container, found, refusal):
    record = {
        "unsettled": TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", VERIFIED, 905,
                                                                            input_hash="a" * 64)),
        "invalid": "not a record\n",
        "another Sage": replace(deployed(container), authority=Authority("ffffffffffffffff", "1111111111111111", container.id)),
    }[found]
    place(container, record)
    before = Path(record_path(config.WORKER_HOME_DIR, container.id)).read_text()
    with pytest.raises(TaskFailed):
      await manager.stop_container(container.id)
    assert Path(record_path(config.WORKER_HOME_DIR, container.id)).read_text() == before
    assert events(container) == [("stop", "rejected", {"refusal": refusal})]

  async def test_a_record_from_before_a_restore_does_not_refuse_and_gives_nothing_to_the_new_authority(
          self, manager, worker_host, container):
    place(container, replace(deployed(container), authority=authority(container, generation="9999999999999999")))
    worker_host.run(container, "running d0")
    await manager.stop_container(container.id)
    record = worker_record(container)
    assert (record.authority, record.applied, record.settled.result) == (authority(container), None, "succeeded")
    assert worker_host.runtime(container) is None

  async def test_a_container_deleted_before_its_turn_is_nothing_to_do(self, manager, worker_host, container):
    container_id = container.id
    container.delete_instance()
    worker_host.reachable = False
    await manager.stop_container(container_id)
    assert ContainerEvent.select().count() == 0


def unsettled(container):
  return TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", VERIFIED, 905, input_hash="a" * 64))


class TestDelete:
  async def test_a_delete_removes_the_placement_then_the_row_and_unlinks_its_domains(
          self, manager, worker_host, container, application, notices):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    domain = Domain.create(name="shop", type="public", port=80, container=container)
    await manager.delete_container(container.id)
    assert notices() == [("success", "Demo / Web on w1 deleted.", PAGE)]

    assert not Path(placement_dir(config.WORKER_HOME_DIR, container.id)).exists()
    assert worker_host.runtime(container) is None
    assert Container.get_or_none(Container.id == container.id) is None
    assert Domain.get_by_id(domain.id).container is None
    assert Containers().target(container.id) == TargetRead("unobserved")
    assert [(action, transition) for action, transition, _ in events(container)] == [("delete", "admitted"), ("delete", "succeeded")]
    assert [line["message"] for line in Logs().query_application_lines("demo-web")["lines"]] == [
        "Removed from this worker (task t1)"]

  @pytest.mark.parametrize("found", ["unsettled", "invalid"])
  async def test_a_delete_does_not_wait_for_old_work(self, manager, worker_host, container, found):
    place(container, unsettled(container) if found == "unsettled" else "not a record\n")
    worker_host.run(container, "running d9")
    await manager.delete_container(container.id)
    assert Container.get_or_none(Container.id == container.id) is None and worker_host.runtime(container) is None

  @pytest.mark.parametrize("found", ["unsettled", "invalid"])
  async def test_a_failed_delete_over_old_work_keeps_its_allowance(self, manager, worker_host, container, found):
    place(container, unsettled(container) if found == "unsettled" else "not a record\n")
    worker_host.run(container, "running d9")
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.delete_container(container.id)
    record = worker_record(container)
    assert (record.operation.contract, record.operation.phase, record.operation.effect_bound_seconds) == (
        "delete", "recovering", 905)
    assert Container.get_or_none(Container.id == container.id) is not None
    assert [(action, transition) for action, transition, _ in events(container)] == [("delete", "admitted"), ("delete", "failed")]

  async def test_an_unreachable_worker_refuses_a_delete_without_force(self, manager, worker_host, container):
    worker_host.reachable = False
    with pytest.raises(TaskFailed):
      await manager.delete_container(container.id)
    assert Container.get_or_none(Container.id == container.id) is not None
    assert events(container) == [("delete", "rejected", {"refusal": "unreachable"})]

  async def test_a_forced_delete_of_a_worker_that_is_gone_removes_only_the_row(self, manager, worker_host, container, notices):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    worker_host.reachable = False
    await manager.delete_container(container.id, force=True)
    assert notices() == [("warning", "Demo / Web on w1 deleted from Sage; the worker could not be reached, so its files stay there.",
                          PAGE)]
    assert Container.get_or_none(Container.id == container.id) is None
    assert (worker_record(container), worker_host.runtime(container)) == (deployed(container), "running d0")
    assert events(container) == [("delete", "admitted", {"worker": "unreachable"}),
                                 ("delete", "succeeded", {"worker": "not cleaned up"})]

  async def test_a_forced_delete_of_a_worker_that_answers_cleans_it(self, manager, worker_host, container):
    place(container, deployed(container))
    await manager.delete_container(container.id, force=True)
    assert not Path(placement_dir(config.WORKER_HOME_DIR, container.id)).exists()

  async def test_another_sages_record_refuses_a_delete(self, manager, worker_host, container):
    place(container, replace(deployed(container), authority=Authority("ffffffffffffffff", "1111111111111111", container.id)))
    with pytest.raises(TaskFailed):
      await manager.delete_container(container.id, force=True)
    assert events(container) == [("delete", "rejected", {"refusal": "authority"})]
    assert worker_record(container) is not None

  async def test_a_failed_delete_keeps_the_row_and_says_why(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    worker_host.flag("down-fails")
    with pytest.raises(TaskFailed):
      await manager.delete_container(container.id)
    record = worker_record(container)
    assert Container.get_or_none(Container.id == container.id) is not None
    assert (record.operation.contract, record.operation.outcome) == ("delete", "failed")
    assert "down failed" in record.operation.reason


class TestDomainApplies:
  @pytest.fixture
  def domains(self, container, application):
    from services.db.models import Worker
    other = Container.create(application=application, worker=Worker.create(hostname="w2", subdomain="blue"))
    Domain.create(name="other", type="public", port=80, container=other)
    return sorted(Domain.create(name=name, type="public", port=80, container=container).id for name in ("shop", "api"))

  async def test_a_deploy_or_a_stop_has_its_containers_domains_applied_when_it_ends(
          self, manager, worker_host, container, domains):
    await deploy(manager, container)
    await manager.stop_container(container.id)
    assert manager.applied == [domains, domains]

  async def test_a_failed_deploy_has_them_applied(self, manager, worker_host, container, domains):
    worker_host.flag("up-fails")
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    assert manager.applied == [domains]

  async def test_a_refused_operation_has_nothing_applied(self, manager, worker_host, container, domains):
    worker_host.reachable = False
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    assert manager.applied == []

  async def test_a_delete_has_the_domains_it_unlinked_applied_and_their_routes_left_on_the_worker_removed(
          self, manager, worker_host, container, domains):
    await manager.delete_container(container.id)
    assert (manager.applied, manager.reconciled) == ([domains], 1)

  async def test_a_delete_that_unlinks_no_domain_asks_for_no_pass(self, manager, worker_host, container):
    await manager.delete_container(container.id)
    assert (manager.applied, manager.reconciled) == ([], 0)


class TestDeleteRequests:
  @pytest.fixture
  def other(self, application):
    from services.db.models import Worker
    return Container.create(application=application, worker=Worker.create(hostname="w2", subdomain="blue"))

  def test_a_delete_cancels_the_containers_queued_tasks_and_no_other(self, manager, container, other):
    manager.request_deploy(container.id)
    kept = manager.request_deploy(other.id)
    delete = manager.request_delete(container.id)
    assert [entry["task_id"] for entry in manager.task_queue.snapshot()["queued"]] == [kept, delete]
    [entry] = [entry for entry in manager.task_queue.snapshot()["queued"] if entry["task_id"] == delete]
    assert (entry["name"], entry["key"], entry["params"]) == (
        "delete", ["delete", container.id], {"container_id": container.id, "force": False})

  async def test_a_delete_waits_for_the_running_operation_instead_of_being_refused(self, manager, container):
    release, ran = asyncio.Event(), []

    async def holds(container_id, deploy):
      await release.wait()
      ran.append("deploy")

    async def deletes(container_id, force):
      ran.append("delete")

    manager.deploy_container, manager.delete_container = holds, deletes
    manager.request_deploy(container.id)
    manager.task_queue.dispatch_tick()
    assert manager.request_delete(container.id)
    release.set()
    await manager.run()
    await manager.run()
    assert ran == ["deploy", "delete"]

  @pytest.mark.parametrize("request_name", ["request_deploy", "request_stop"])
  def test_deploy_and_stop_are_refused_while_it_is_being_deleted(self, manager, container, request_name):
    manager.request_delete(container.id)
    with pytest.raises(Conflict, match="being deleted"):
      getattr(manager, request_name)(container.id)

  def test_a_request_for_all_is_refused_while_a_container_is_being_deleted(self, manager, application, container, other):
    manager.request_delete(other.id)
    with pytest.raises(Conflict, match="has an operation in progress"):
      manager.request_deploy_all(application.id)

  async def test_a_forced_delete_asked_while_a_delete_runs_waits_for_it(self, manager, container):
    release, given = asyncio.Event(), []

    async def deletes(container_id, force):
      given.append(force)
      if not force:
        await release.wait()

    manager.delete_container = deletes
    manager.request_delete(container.id)
    manager.task_queue.dispatch_tick()
    await asyncio.sleep(0)
    assert manager.request_delete(container.id, force=True)
    release.set()
    await manager.run()
    await manager.run()
    assert given == [False, True]

  def test_a_forced_delete_replaces_a_waiting_one(self, manager, container):
    manager.request_delete(container.id)
    manager.request_delete(container.id, force=True)
    assert [entry["params"]["force"] for entry in manager.task_queue.snapshot()["queued"]] == [True]


class TestRecovery:
  async def test_containment_stops_it_and_keeps_the_record_recovering_with_the_old_works_allowance(
          self, manager, worker_host, container):
    place(container, replace(unsettled(container), applied=Applied("d0", "a" * 64, VERIFIED)))
    worker_host.run(container, "running d9")
    await manager.recover_container(container.id, "d9", final=False)
    record = worker_record(container)
    assert worker_host.runtime(container) is None
    assert (record.operation.id, record.operation.phase, record.operation.outcome, record.operation.recovery,
            record.operation.effect_bound_seconds) == ("d9", "recovering", "interrupted", Recovery("unresolved"), 905)
    assert events(container) == [("deploy", "interrupted", None)]

  async def test_settling_after_the_allowance_records_the_interruption_and_lets_a_deploy_run(
          self, manager, worker_host, container, notices):
    place(container, replace(unsettled(container), applied=Applied("d0", "a" * 64, VERIFIED)))
    await manager.recover_container(container.id, "d9", final=False)
    assert notices() == []
    worker_host.run(container, "running d9")
    await manager.recover_container(container.id, "d9", final=True)
    assert notices() == [("warning", "Demo / Web on w1 was stopped after an interrupted deploy.", PAGE)]
    record = worker_record(container)
    assert worker_host.runtime(container) is None
    assert (record.settled.operation_id, record.settled.runtime_goal, record.settled.result, record.settled.reason) == (
        "d9", "stopped", "failed", "its deploy was interrupted")
    assert (record.operation.phase, record.operation.recovery, record.operation.verified_on_worker_at) == (
        "settled", Recovery("verified"), VERIFIED)
    assert record.applied == Applied("d0", "a" * 64, VERIFIED)
    assert [transition for _, transition, _ in events(container)] == ["interrupted", "recovered"]
    await deploy(manager, container)
    assert worker_record(container).settled.result == "succeeded"

  async def test_an_unreadable_record_is_replaced_by_a_reset_that_waits_the_longest_allowance(
          self, manager, worker_host, container):
    place(container, "not a record\n")
    worker_host.run(container, "running d9")
    await manager.recover_container(container.id, "unreadable", final=False)
    record = worker_record(container)
    assert (record.authority, record.operation.contract, record.operation.phase, record.operation.effect_bound_seconds) == (
        authority(container), "reset", "recovering", 905)
    await manager.recover_container(container.id, record.operation.id, final=True)
    assert worker_record(container).settled.result == "failed" and worker_record(container).operation.phase == "settled"

  async def test_a_record_from_before_a_restore_settles_under_the_current_authority_without_its_proof(
          self, manager, worker_host, container):
    place(container, replace(unsettled(container), authority=authority(container, generation="9999999999999999"),
                             applied=Applied("d0", "a" * 64, VERIFIED)))
    await manager.recover_container(container.id, "d9", final=True)
    record = worker_record(container)
    assert (record.authority, record.applied, record.settled.result) == (authority(container), None, "failed")

  async def test_a_recovery_the_record_no_longer_needs_does_nothing(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    await manager.recover_container(container.id, "d9", final=True)
    assert (worker_record(container), worker_host.runtime(container), events(container)) == (deployed(container), "running d0", [])

  async def test_a_recovery_of_one_incident_leaves_a_newer_one_to_its_own_wait(self, manager, worker_host, container):
    newer = TargetRecord(authority(container), operation=Operation("d10", "deploy", "executing", VERIFIED, 905, input_hash="b" * 64))
    place(container, newer)
    worker_host.run(container, "running d10")
    await manager.recover_container(container.id, "d9", final=True)
    assert (worker_record(container), worker_host.runtime(container)) == (newer, "running d10")

  async def test_a_recovery_that_cannot_stop_it_is_a_failure_and_leaves_the_record(self, manager, worker_host, container):
    place(container, unsettled(container))
    worker_host.run(container, "running d9")
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.recover_container(container.id, "d9", final=False)
    assert worker_record(container) == unsettled(container)
    assert [(action, transition) for action, transition, _ in events(container)] == [("deploy", "recovery_failed")]


class TestReset:
  async def test_a_runtime_running_after_a_stop_is_stopped_and_the_cause_recorded(self, manager, worker_host, container, notices):
    stopped = replace(deployed(container), settled=Settled("s1", "stopped", "succeeded", VERIFIED))
    place(container, stopped)
    worker_host.run(container, "running d0")
    await manager.reset_container(container.id, "unexpected_running")
    assert notices() == [("warning", "Demo / Web on w1 was stopped: it ran after it was stopped.", PAGE)]
    record = worker_record(container)
    assert worker_host.runtime(container) is None
    assert (record.applied, record.settled.result, record.settled.reason) == (stopped.applied, "failed", "it ran after it was stopped")
    assert (record.operation.contract, record.operation.phase, record.operation.outcome) == ("reset", "settled", "succeeded")
    assert events(container) == [("reset", "admitted", {"cause": "unexpected_running"}),
                                 ("reset", "succeeded", {"cause": "unexpected_running"})]
    assert [line["message"] for line in Logs().query_application_lines("demo-web")["lines"]] == [
        "Stopped: it ran after it was stopped (task t1)"]

  async def test_a_record_from_before_a_restore_is_reset_without_its_proof(self, manager, worker_host, container):
    place(container, replace(deployed(container), authority=authority(container, generation="9999999999999999")))
    worker_host.run(container, "running d0", labelled=authority(container, generation="9999999999999999"))
    await manager.reset_container(container.id, "restored")
    record = worker_record(container)
    assert (record.authority, record.applied, record.settled.reason) == (
        authority(container), None, "its record is from before a platform restore")
    assert worker_host.runtime(container) is None

  async def test_a_runtime_without_a_record_is_stopped(self, manager, worker_host, container):
    worker_host.run(container, "running d5")
    await manager.reset_container(container.id, "no_record")
    assert worker_host.runtime(container) is None and worker_record(container).settled.reason == "it ran without a record"

  async def test_a_runtime_checked_again_that_agrees_with_its_record_is_left_running(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    await manager.reset_container(container.id, "unexpected_deployment")
    assert (worker_host.runtime(container), worker_record(container), events(container)) == ("running d0", deployed(container), [])

  async def test_another_sages_runtime_is_left_alone(self, manager, worker_host, container):
    worker_host.run(container, "running d5", labelled=Authority("ffffffffffffffff", "1111111111111111", container.id))
    await manager.reset_container(container.id, "no_record")
    assert worker_host.runtime(container) == "running d5" and worker_record(container) is None

  async def test_a_reset_that_cannot_stop_it_is_left_recovering(self, manager, worker_host, container):
    place(container, replace(deployed(container), settled=Settled("s1", "stopped", "succeeded", VERIFIED)))
    worker_host.run(container, "running d0")
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.reset_container(container.id, "unexpected_running")
    record = worker_record(container)
    assert (record.operation.contract, record.operation.phase) == ("reset", "recovering")


ORPHAN = "0f0f0f0f0f0f0f0f"


@pytest.fixture
def monotonic(monkeypatch):
  import services.manager.containers as module
  clock = SimpleNamespace(now=1000.0)
  monkeypatch.setattr(module, "monotonic", lambda: clock.now)
  return clock


def orphan_placement(worker_host, record=None, running=True, labelled=None):
  """What a container this Sage has no row for left on the worker."""
  orphan = SimpleNamespace(id=ORPHAN)
  directory = Path(placement_dir(config.WORKER_HOME_DIR, ORPHAN))
  (directory / "volumes" / "1111111111111111").mkdir(parents=True)
  if record is not None:
    (directory / "record.json").write_text(record if isinstance(record, str) else encode_record(record))
  if running:
    worker_host.run(orphan, "running d1", labelled=labelled or authority(orphan))
  return directory


class TestOrphanCleanup:
  async def test_an_orphans_runtime_and_placement_are_removed(self, manager, worker_host, container, notices):
    directory = orphan_placement(worker_host, TargetRecord(authority(SimpleNamespace(id=ORPHAN)),
                                                           settled=Settled("d1", "running", "succeeded", VERIFIED)))
    await manager.cleanup_orphan("w1", ORPHAN)
    assert not directory.exists() and worker_host.runtime(SimpleNamespace(id=ORPHAN)) is None
    assert notices() == [("warning", "Removed a container Sage no longer has from worker w1.", f"{MANAGER}/workers/w1")]

  async def test_a_bare_directory_is_removed(self, manager, worker_host, container, notices):
    directory = orphan_placement(worker_host, running=False)
    await manager.cleanup_orphan("w1", ORPHAN)
    assert not directory.exists()
    assert notices() == [("warning", "Removed a container Sage no longer has from worker w1.", f"{MANAGER}/workers/w1")]

  async def test_a_cleanup_with_nothing_left_stops_nothing_and_sends_no_notice(self, manager, worker_host, container, notices):
    worker_host.flag("down-fails")
    await manager.cleanup_orphan("w1", ORPHAN)
    assert notices() == [] and manager.orphans == {}

  async def test_a_cleanup_asked_for_on_a_report_from_before_a_delete_finds_nothing_and_says_nothing(
          self, manager, worker_host, container, notices):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    await manager.delete_container(container.id)
    await manager.cleanup_orphan("w1", container.id)
    assert notices() == [("success", "Demo / Web on w1 deleted.", PAGE)]

  async def test_a_container_that_has_its_row_is_not_an_orphan(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    await manager.cleanup_orphan("w1", container.id)
    assert (worker_record(container), worker_host.runtime(container)) == (deployed(container), "running d0")

  @pytest.mark.parametrize("whose", ["record", "runtime"])
  async def test_another_sages_placement_is_left_alone(self, manager, worker_host, container, whose):
    foreign = Authority("ffffffffffffffff", "1111111111111111", ORPHAN)
    directory = orphan_placement(worker_host, TargetRecord(foreign) if whose == "record" else None,
                                 labelled=foreign if whose == "runtime" else None)
    await manager.cleanup_orphan("w1", ORPHAN)
    assert directory.exists() and worker_host.runtime(SimpleNamespace(id=ORPHAN)) == "running d1"

  @pytest.mark.parametrize("record, bound", [
      (TargetRecord(Authority("0" * 16, "0" * 16, ORPHAN), operation=Operation("d9", "deploy", "executing", VERIFIED, 905,
                                                                               input_hash="a" * 64)), 905),
      ("not a record\n", 905)], ids=["unsettled", "unreadable"])
  async def test_old_work_on_an_orphan_is_waited_out_from_when_it_was_seen(
          self, manager, worker_host, container, monotonic, record, bound):
    if not isinstance(record, str):
      record = replace(record, authority=authority(SimpleNamespace(id=ORPHAN)))
    directory = orphan_placement(worker_host, record)
    await manager.cleanup_orphan("w1", ORPHAN)
    monotonic.now += bound - 1
    await manager.cleanup_orphan("w1", ORPHAN)
    assert directory.exists() and manager.orphans[("w1", ORPHAN)][2] == bound
    monotonic.now += 1
    await manager.cleanup_orphan("w1", ORPHAN)
    assert not directory.exists() and manager.orphans == {}

  async def test_an_orphan_that_cannot_be_stopped_is_a_failure_and_stays(self, manager, worker_host, container):
    directory = orphan_placement(worker_host)
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.cleanup_orphan("w1", ORPHAN)
    assert directory.exists()


class TestMark:
  def lines(self, application):
    return [(line["hostname"], line["stream"], line["message"])
            for line in Logs().query_application_lines("demo-web")["lines"]]

  async def test_it_writes_what_sage_did_with_the_task_into_the_containers_application_logs(self, container, application):
    await mark(container, "Deployed")
    assert self.lines(application) == [("w1", "sage", "Deployed (task t1)")]

  async def test_outside_a_task_it_is_the_text_alone(self, container, application):
    token = task_id.set("")
    try:
      await mark(container, "Stopped")
    finally:
      task_id.reset(token)
    assert self.lines(application) == [("w1", "sage", "Stopped")]

  async def test_a_marker_that_cannot_be_written_is_a_warning_and_not_the_actions_failure(
          self, container, monkeypatch, caplog):
    def broken(self, *args):
      raise OSError("disk full")

    monkeypatch.setattr(Logs, "write_marker", broken)
    await mark(container, "Deployed")
    assert "Could not mark 'Deployed' in the logs of demo-web." in caplog.text


class TestRequests:
  def queued(self, manager):
    return [{name: entry[name] for name in ("name", "key", "scopes", "params", "labels")}
            for entry in manager.task_queue.snapshot()["queued"]]

  def test_a_deploy_takes_the_containers_lifecycle_slot_with_what_people_call_it(self, manager, container):
    task = manager.request_deploy(container.id)
    assert manager.task_queue.snapshot()["queued"][0]["task_id"] == task
    assert self.queued(manager) == [{
        "name": "deploy", "key": ["lifecycle", container.id], "scopes": [f"worker:w1:container:{container.id}"],
        "params": {"container_id": container.id}, "labels": {"Application": "Demo / Web", "Worker": "w1"}}]

  def test_the_latest_request_replaces_the_one_waiting(self, manager, container):
    manager.request_deploy(container.id)
    manager.request_stop(container.id)
    assert [entry["name"] for entry in self.queued(manager)] == ["stop"]

  async def test_a_request_while_one_runs_is_refused(self, manager, container):
    release = asyncio.Event()

    async def holds(container_id, deploy):
      await release.wait()

    manager.deploy_container = holds
    manager.request_deploy(container.id)
    manager.task_queue.dispatch_tick()
    with pytest.raises(Conflict, match="already has an operation in progress"):
      manager.request_stop(container.id)
    release.set()
    await manager.run()

  @pytest.mark.parametrize("change, message", [
      ({"image": None}, "must have an image"), ({"type": "git", "image": None, "repo": None}, "must have a repo")])
  def test_a_deploy_of_inputs_that_cannot_run_is_refused(self, manager, container, application, change, message):
    Application.update(**change).where(Application.id == application.id).execute()
    with pytest.raises(ValueError, match=message):
      manager.request_deploy(container.id)
    assert self.queued(manager) == []

  def test_a_request_for_a_missing_container_is_refused(self, manager):
    with pytest.raises(LookupError):
      manager.request_stop("0123456789abcdef")


class TestRequestsForAll:
  @pytest.fixture
  def containers(self, application, worker):
    from services.db.models import Worker
    second = Worker.create(hostname="w2", subdomain="blue")
    return [Container.create(application=application, worker=host) for host in (worker, second)]

  async def test_deploy_all_gives_each_container_its_own_task_and_the_inputs_of_the_request(
          self, manager, application, containers):
    given = {}

    async def records(container_id, deploy):
      given[container_id] = deploy

    manager.deploy_container = records
    tasks = manager.request_deploy_all(application.id)
    Application.update(image="nginx:2").where(Application.id == application.id).execute()
    await manager.run()
    assert len(set(tasks)) == 2 and set(given) == {container.id for container in containers}
    assert {deploy.image for deploy in given.values()} == {"nginx"}

  def test_a_request_for_all_is_refused_while_any_container_has_one(self, manager, application, containers):
    manager.request_stop(containers[1].id)
    for request in (manager.request_deploy_all, manager.request_stop_all):
      with pytest.raises(Conflict, match="has an operation in progress"):
        request(application.id)
    assert len(manager.task_queue.snapshot()["queued"]) == 1

  def test_stop_all_stops_each_container(self, manager, application, containers):
    assert len(manager.request_stop_all(application.id)) == 2
    assert {tuple(entry["key"]) for entry in manager.task_queue.snapshot()["queued"]} == {
        ("lifecycle", container.id) for container in containers}

  @pytest.mark.parametrize("request_name", ["request_deploy_all", "request_stop_all"])
  def test_an_application_without_containers_is_refused(self, manager, application, request_name):
    with pytest.raises(ValueError, match="no containers"):
      getattr(manager, request_name)(application.id)


class TestCancel:
  def test_a_queued_request_is_cancelled_by_its_operation(self, manager, container):
    task = manager.request_deploy(container.id)
    with pytest.raises(LookupError, match="no queued stop"):
      manager.request_cancel(container.id, "stop")
    assert manager.request_cancel(container.id, "deploy") == task
    assert manager.task_queue.snapshot()["queued"] == []

  def test_a_queued_delete_is_cancelled(self, manager, container):
    task = manager.request_delete(container.id)
    assert manager.request_cancel(container.id, "delete") == task
    assert manager.task_queue.snapshot()["queued"] == []

  async def test_a_started_operation_is_not_cancelled(self, manager, container):
    release = asyncio.Event()

    async def holds(container_id):
      await release.wait()

    manager.stop_container = holds
    manager.request_stop(container.id)
    manager.task_queue.dispatch_tick()
    with pytest.raises(Conflict, match="The stop of this container has already started"):
      manager.request_cancel(container.id, "stop")
    release.set()
    await manager.run()

  def test_nothing_queued_is_nothing_to_cancel(self, manager, container):
    with pytest.raises(LookupError, match="no queued deploy"):
      manager.request_cancel(container.id, "deploy")
    with pytest.raises(ValueError):
      manager.request_cancel(container.id, "backup")
