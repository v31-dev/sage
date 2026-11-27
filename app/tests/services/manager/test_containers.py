import asyncio
import os
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services.applications.deploy_input import capture_deploy_config
from services.containers import commands
from services.containers.placement import placement_dir, placement_unit, record_path, runtime_name
from services.containers.record import Applied, Authority, Operation, Recovery, Settled, TargetRead, TargetRecord, decode_record, encode_record
from services.containers.rendering import deployment_hash
from services.containers.targets import Containers
from services.db.models import Application, Container, ContainerEvent, Domain, Instance, Volume
from services.logs import Logs
from services.manager.containers import ContainersMixin
from services.manager.lifecycle import LifecycleMixin, mark
from services.manager.queue import SCOPES, TASK_NAMES, QueueMixin
from services.tailscale import EX_TEMPFAIL, CommandError, Tailscale
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
    label() { sed -n "s/^ *sage.$1: \\"\\(.*\\)\\"$/\\1/p" "$file" | head -n 1; }
    deployment=$(label deployment)
    [ -e "$state/runs-another" ] && deployment=another
    labels="$deployment|$(label instance)|$(label generation)|$(label container)"
    echo "running|$labels" > "$state/runtime-$name"
    [ -e "$state/up-fails" ] && { echo "pull access denied"; exit 1; }
    [ -e "$state/exits" ] && echo "exited|$labels" > "$state/runtime-$name"
    ;;
  inspect*)
    [ -e "$state/runtime-$last" ] || { echo; echo "Error: No such object: $last" >&2; exit 1; }
    cat "$state/runtime-$last"
    ;;
esac
exit 0
"""

SYSTEMCTL = """#!/bin/sh
# The worker's systemd: a file `unit-<name>` under $FAKE_DOCKER for each active unit; `units-stay` keeps them active
# through a stop.
state="$FAKE_DOCKER"
for last; do :; done
case "$1" in
  stop) [ -e "$state/units-stay" ] || rm -f "$state"/unit-$last ;;
  is-active) for unit in "$state"/unit-$last; do [ -e "$unit" ] && exit 0; done; exit 4 ;;
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
    for name, script in (("docker", DOCKER), ("systemctl", SYSTEMCTL), ("date", f"#!/bin/sh\necho {WORKER_CLOCK}\n")):
      (binaries / name).write_text(script)
      (binaries / name).chmod(stat.S_IRWXU)
    self.env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "FAKE_DOCKER": str(self.state)}
    self.reachable = True
    self.breaks_at = None
    self.records_fail = False
    self.loses = None

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

  def leave_unit(self, container_id):
    """A unit of the placement's runtime commands, left active."""
    (self.state / f"unit-{placement_unit(container_id)}-{len(self.units())}").touch()

  def units(self) -> list[str]:
    return sorted(path.name.removeprefix("unit-") for path in self.state.glob("unit-*"))

  def ssh(self, hostname):
    return "reachable" if self.reachable else "unreachable"

  def answer(self, what):
    if self.breaks_at is not None and self.breaks_at in what:
      self.reachable = False
    if not self.reachable:
      raise CommandError("Error connecting to w1: no route", transient=True)

  async def exec_command(self, hostname, command, timeout=300, unit=None):
    """Runs the command; a tracked one holding `loses` instead loses its connection, its unit not stopped."""
    self.answer(command)
    if unit is not None and self.loses is not None and self.loses in command:
      raise CommandError(f"Error executing command on {hostname}: gone It may still be running.", running=True)
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=self.env)
    if result.returncode:
      raise CommandError(f"Error executing command on {hostname}: exit code {result.returncode}\n{result.stdout}",
                         transient=result.returncode == EX_TEMPFAIL)
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
    self.applied, self.reconciled, self.traefik_networks = [], 0, []
    self.task_queue = TaskQueue(SCOPES, names=TASK_NAMES, executors={"main": self.pool}, record=lambda task, status: None, notify=self._queue_changed)

  def request_domain_applies(self, domain_ids):
    if ids := sorted(domain_ids):
      self.applied.append(ids)

  def request_domain_reconciliation(self):
    self.reconciled += 1

  def request_traefik_networks(self, hostname):
    self.traefik_networks.append(hostname)

  async def run(self):
    self.task_queue.dispatch_tick()
    while self.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
      self.task_queue.dispatch_tick()


@pytest.fixture
def worker_host(register, tmp_path, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", str(tmp_path / "home"))
  monkeypatch.setattr(config, "WORKER_TMP_DIR", str(tmp_path / "home" / "tmp"))
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
                      Operation("d0", "deploy", "settled", at, at, "a" * 64, outcome="succeeded", verified_on_worker_at=at))


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
    assert record.applied.config_hash == deployment_hash(capture_deploy_config(container.application_id))
    assert (record.settled.operation_id, record.settled.runtime_goal, record.settled.result) == (operation.id, "running", "succeeded")
    assert operation.verified_on_worker_at == VERIFIED
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

  async def test_a_deploy_whose_command_may_still_run_is_left_recovering_without_a_stop(self, manager, worker_host, container):
    worker_host.loses = "up -d"
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    record = worker_record(container)
    assert (record.operation.phase, record.operation.outcome, record.settled) == ("recovering", "failed", None)
    assert "may still be running" in record.operation.reason

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
    assert (record.operation.contract, record.operation.phase, record.operation.verified_on_worker_at) == (
        "stop", "settled", VERIFIED)
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
        "unsettled": TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", VERIFIED,
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
  return TargetRecord(authority(container), operation=Operation("d9", "deploy", "executing", VERIFIED, input_hash="a" * 64))


class TestDelete:
  async def test_a_delete_removes_the_placement_then_the_row_with_its_domains(
          self, manager, worker_host, container, application, notices):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    domain = Domain.create(name="shop", type="public", port=80, container=container)
    await manager.delete_container(container.id)
    assert notices() == [("success", "Demo / Web on w1 deleted.", PAGE)]

    assert not Path(placement_dir(config.WORKER_HOME_DIR, container.id)).exists()
    assert worker_host.runtime(container) is None
    assert Container.get_or_none(Container.id == container.id) is None
    assert Domain.get_or_none(Domain.id == domain.id) is None
    assert Containers().target(container.id) == TargetRead("unobserved")
    assert [(action, transition) for action, transition, _ in events(container)] == [("delete", "admitted"), ("delete", "succeeded")]
    assert [line["message"] for line in Logs().query_application_lines("demo-web")["lines"]] == [
        "Removed from this worker (task t1)"]

  @pytest.mark.parametrize("found", ["unsettled", "invalid"])
  async def test_a_delete_stops_what_old_work_left_running_and_does_not_wait_for_it(
          self, manager, worker_host, container, found):
    place(container, unsettled(container) if found == "unsettled" else "not a record\n")
    worker_host.run(container, "running d9")
    worker_host.leave_unit(container.id)
    await manager.delete_container(container.id)
    assert Container.get_or_none(Container.id == container.id) is None and worker_host.runtime(container) is None
    assert worker_host.units() == []

  async def test_a_delete_whose_old_work_cannot_be_stopped_runs_nothing_beside_it(self, manager, worker_host, container):
    place(container, unsettled(container))
    worker_host.run(container, "running d9")
    worker_host.leave_unit(container.id)
    worker_host.flag("units-stay")
    with pytest.raises(TaskFailed):
      await manager.delete_container(container.id)
    record = worker_record(container)
    assert (record.operation.contract, record.operation.phase, worker_host.runtime(container)) == (
        "delete", "recovering", "running d9")
    assert Container.get_or_none(Container.id == container.id) is not None

  @pytest.mark.parametrize("found", ["unsettled", "invalid"])
  async def test_a_failed_delete_over_old_work_is_left_recovering(self, manager, worker_host, container, found):
    place(container, unsettled(container) if found == "unsettled" else "not a record\n")
    worker_host.run(container, "running d9")
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.delete_container(container.id)
    record = worker_record(container)
    assert (record.operation.contract, record.operation.phase) == ("delete", "recovering")
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

  async def test_a_deploy_and_a_delete_ask_for_traefiks_networks_when_they_end_and_a_failed_deploy_does_not(
          self, manager, worker_host, container):
    worker_host.flag("up-fails")
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    assert manager.traefik_networks == []
    (worker_host.state / "up-fails").unlink()
    await deploy(manager, container)
    await manager.delete_container(container.id)
    assert manager.traefik_networks == ["w1", "w1"]

  async def test_a_delete_of_a_container_with_domains_has_every_workers_callers_follow_it(
          self, manager, worker_host, container, domains):
    await deploy(manager, container)
    manager.traefik_networks.clear()
    await manager.delete_container(container.id)
    assert manager.traefik_networks == [None]

  @pytest.fixture
  def allowing(self, container, project):
    """An internal Domain of another application, on another worker, that allows the container's application."""
    from services.db.models import Application, DomainCaller, Worker
    other = Application.create(project=project, name="pg", label="PG", image="postgres")
    target = Container.create(application=other, worker=Worker.create(hostname="w9", subdomain="grey"))
    domain = Domain.create(name="pg", type="tcp", port=5432, container=target, namespace_worker="w9")
    DomainCaller.create(domain=domain, application=container.application)
    return domain.id

  async def test_a_deploy_a_stop_a_failure_and_a_delete_have_the_domains_allowing_its_application_applied(
          self, manager, worker_host, container, allowing):
    await deploy(manager, container)
    await manager.stop_container(container.id)
    worker_host.flag("up-fails")
    with pytest.raises(TaskFailed):
      await deploy(manager, container)
    (worker_host.state / "up-fails").unlink()
    await manager.delete_container(container.id)
    assert manager.applied == [[allowing]] * 4

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

  async def test_a_delete_with_domains_asks_the_pass_to_remove_their_routes_and_records(
          self, manager, worker_host, container, domains):
    await manager.delete_container(container.id)
    assert (manager.applied, manager.reconciled) == ([], 1)

  async def test_a_delete_without_domains_asks_for_no_pass(self, manager, worker_host, container):
    await manager.delete_container(container.id)
    assert (manager.applied, manager.reconciled) == ([], 0)

  async def test_a_delete_of_a_container_a_domain_allows_asks_the_pass_to_remove_that_domains_file_on_its_worker(
          self, manager, worker_host, container, allowing):
    await manager.delete_container(container.id)
    assert (manager.applied, manager.reconciled) == ([[allowing]], 1)


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

  @pytest.mark.parametrize("resume", [False, True], ids=["past a check that let it through", "resumed"])
  def test_the_queue_itself_refuses_a_request_while_it_is_being_deleted(self, manager, container, monkeypatch, resume):
    delete = manager.request_delete(container.id)
    monkeypatch.setattr(manager, "request_refusal", lambda container: None)
    with pytest.raises(Conflict, match="^This container is being deleted.$"):
      manager.request_deploy(container.id, resume=resume)
    assert [entry["task_id"] for entry in manager.task_queue.snapshot()["queued"]] == [delete]
    assert intent(container) == "delete"

  def test_a_request_for_all_is_refused_while_a_container_is_being_deleted(self, manager, application, container, other):
    manager.request_delete(other.id)
    with pytest.raises(Conflict, match="^A container of this application can't take this request: it is being deleted.$"):
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


def intent(container):
  return Container.get_by_id(container.id).intent


class TestIntent:
  def test_a_request_records_what_was_asked_and_a_later_one_replaces_it(self, manager, container):
    manager.request_deploy(container.id)
    assert intent(container) == "deploy"
    manager.request_stop(container.id)
    assert intent(container) == "stop"
    manager.request_delete(container.id)
    assert intent(container) == "delete"

  async def test_a_request_the_queue_does_not_take_writes_none_and_the_running_one_clears_its_own(
          self, manager, container, monkeypatch):
    release, written = asyncio.Event(), []
    record_intent = commands.record_intent

    async def holds(container_id, deploy):
      await release.wait()

    def recording(container_id, name):
      written.append(name)
      record_intent(container_id, name)

    monkeypatch.setattr(commands, "record_intent", recording)
    manager.deploy_container = holds
    manager.request_deploy(container.id)
    manager.task_queue.dispatch_tick()
    await asyncio.sleep(0)
    with pytest.raises(Conflict):
      manager.request_stop(container.id)
    assert (written, intent(container)) == (["deploy"], "deploy")
    release.set()
    await manager.run()
    assert intent(container) is None

  def test_while_a_request_writes_its_intent_its_task_is_queued_and_cannot_start_so_a_resume_joins_it(
          self, manager, container, monkeypatch):
    entered, release, answers = threading.Event(), threading.Event(), {}
    record_intent = commands.record_intent

    def slow(container_id, name):
      record_intent(container_id, name)
      entered.set()
      release.wait(5)

    def ask(label, **options):
      answers[label] = manager.request_deploy(container.id, **options)

    monkeypatch.setattr(commands, "record_intent", slow)
    route = threading.Thread(target=ask, args=("route",))
    route.start()
    assert entered.wait(5)
    manager.task_queue.dispatch_tick()
    assert intent(container) == "deploy" and manager.has_task(("lifecycle", container.id))
    assert manager.task_queue.snapshot()["running"] == []
    resume = threading.Thread(target=ask, args=("resume",), kwargs={"resume": True})
    resume.start()
    release.set()
    route.join(5)
    resume.join(5)
    assert answers["resume"] == answers["route"]
    assert [entry["task_id"] for entry in manager.task_queue.snapshot()["queued"]] == [answers["route"]]

  async def test_a_deploy_that_succeeds_clears_it(self, manager, worker_host, container):
    manager.request_deploy(container.id)
    await manager.run()
    assert worker_record(container).settled.result == "succeeded" and intent(container) is None

  async def test_a_failed_or_refused_request_clears_it(self, manager, container):
    async def fails(container_id):
      raise TaskFailed()

    manager.stop_container = fails
    manager.request_stop(container.id)
    await manager.run()
    assert intent(container) is None

  async def test_a_request_cut_off_by_the_shutdown_keeps_it(self, manager, container):
    async def cut_off(container_id):
      raise asyncio.CancelledError()

    manager.stop_container = cut_off
    manager.request_stop(container.id)
    await manager.run()
    assert intent(container) == "stop"

  async def test_a_delete_asked_while_a_deploy_runs_keeps_its_own_once_the_deploy_ends(self, manager, container):
    release, deleting = asyncio.Event(), asyncio.Event()

    async def holds(container_id, deploy):
      await release.wait()

    async def deletes(container_id, force):
      assert intent(container) == "delete"
      deleting.set()

    manager.deploy_container, manager.delete_container = holds, deletes
    manager.request_deploy(container.id)
    manager.task_queue.dispatch_tick()
    await asyncio.sleep(0)
    manager.request_delete(container.id)
    release.set()
    await manager.run()
    await manager.run()
    assert deleting.is_set() and intent(container) is None

  def test_cancelling_the_queued_request_clears_it(self, manager, container):
    manager.request_deploy(container.id)
    manager.request_cancel(container.id, "deploy")
    assert intent(container) is None


class TestRecovery:
  async def test_it_stops_what_the_operation_left_running_and_the_container_then_settles_it_and_lets_a_deploy_run(
          self, manager, worker_host, container, notices):
    place(container, replace(unsettled(container), applied=Applied("d0", "a" * 64, VERIFIED)))
    worker_host.run(container, "running d9")
    worker_host.leave_unit(container.id)
    worker_host.leave_unit(ORPHAN)
    await manager.recover_container(container.id, "d9")
    assert notices() == [("warning", "Demo / Web on w1 was stopped after an interrupted deploy.", PAGE)]
    assert worker_host.units() == [f"{placement_unit(ORPHAN)}-1"] and worker_host.runtime(container) is None
    record = worker_record(container)
    assert (record.settled.operation_id, record.settled.runtime_goal, record.settled.result, record.settled.reason) == (
        "d9", "stopped", "failed", "its deploy was interrupted")
    assert (record.operation.phase, record.operation.outcome, record.operation.recovery,
            record.operation.verified_on_worker_at) == ("settled", "interrupted", Recovery("verified"), VERIFIED)
    assert record.applied == Applied("d0", "a" * 64, VERIFIED)
    assert [transition for _, transition, _ in events(container)] == ["interrupted", "recovered"]
    await deploy(manager, container)
    assert worker_record(container).settled.result == "succeeded"

  async def test_an_unreadable_record_is_settled_as_a_reset(self, manager, worker_host, container):
    place(container, "not a record\n")
    worker_host.run(container, "running d9")
    await manager.recover_container(container.id, "unreadable")
    record = worker_record(container)
    assert (record.authority, record.operation.contract, record.operation.phase, record.settled.result,
            record.settled.reason) == (authority(container), "reset", "settled", "failed", "its record could not be read")
    assert worker_host.runtime(container) is None

  async def test_a_record_from_before_a_restore_settles_under_the_current_authority_without_its_proof(
          self, manager, worker_host, container):
    place(container, replace(unsettled(container), authority=authority(container, generation="9999999999999999"),
                             applied=Applied("d0", "a" * 64, VERIFIED)))
    await manager.recover_container(container.id, "d9")
    record = worker_record(container)
    assert (record.authority, record.applied, record.settled.result) == (authority(container), None, "failed")

  async def test_a_recovery_the_record_no_longer_needs_does_nothing(self, manager, worker_host, container):
    place(container, deployed(container))
    worker_host.run(container, "running d0")
    await manager.recover_container(container.id, "d9")
    assert (worker_record(container), worker_host.runtime(container), events(container)) == (deployed(container), "running d0", [])

  async def test_a_recovery_of_one_incident_leaves_a_newer_one_to_its_own_wait(self, manager, worker_host, container):
    newer = TargetRecord(authority(container), operation=Operation("d10", "deploy", "executing", VERIFIED, input_hash="b" * 64))
    place(container, newer)
    worker_host.run(container, "running d10")
    await manager.recover_container(container.id, "d9")
    assert (worker_record(container), worker_host.runtime(container)) == (newer, "running d10")

  async def test_a_recovery_that_cannot_stop_it_is_a_failure_and_leaves_the_record(self, manager, worker_host, container):
    place(container, unsettled(container))
    worker_host.run(container, "running d9")
    worker_host.flag("down-is-ignored")
    with pytest.raises(TaskFailed):
      await manager.recover_container(container.id, "d9")
    assert worker_record(container) == unsettled(container)
    assert [(action, transition) for action, transition, _ in events(container)] == [
        ("deploy", "interrupted"), ("deploy", "recovery_failed")]

  async def test_a_recovery_that_cannot_stop_what_the_operation_left_running_runs_nothing_beside_it(
          self, manager, worker_host, container):
    place(container, unsettled(container))
    worker_host.run(container, "running d9")
    worker_host.leave_unit(container.id)
    worker_host.flag("units-stay")
    with pytest.raises(TaskFailed):
      await manager.recover_container(container.id, "d9")
    assert (worker_record(container), worker_host.runtime(container)) == (unsettled(container), "running d9")
    assert [transition for _, transition, _ in events(container)] == ["interrupted", "recovery_failed"]


class TestRequestsWhileRecovering:
  @pytest.fixture
  async def recovering(self, manager, worker_host, container):
    place(container, unsettled(container))
    await Containers().read(container)
    return container

  @pytest.fixture
  def other(self, application):
    from services.db.models import Worker
    return Container.create(application=application, worker=Worker.create(hostname="w2", subdomain="blue"))

  async def test_deploy_and_stop_are_refused_before_queueing_and_delete_is_admitted(self, manager, recovering):
    with pytest.raises(Conflict, match="^Deploy refused: an earlier operation on it is still being recovered.$"):
      manager.request_deploy(recovering.id)
    with pytest.raises(Conflict, match="^Stop refused: an earlier operation on it is still being recovered.$"):
      manager.request_stop(recovering.id)
    assert (manager.task_queue.snapshot()["queued"], intent(recovering)) == ([], None)
    assert manager.request_delete(recovering.id)

  async def test_an_unreadable_record_refuses_them_with_its_reason(self, manager, worker_host, container):
    place(container, "not a record\n")
    await Containers().read(container)
    with pytest.raises(Conflict, match="^Deploy refused: its record on the worker can't be read.$"):
      manager.request_deploy(container.id)

  async def test_once_the_recovery_settles_a_deploy_is_admitted(self, manager, worker_host, recovering):
    await manager.recover_container(recovering.id, "d9")
    assert manager.request_deploy(recovering.id)

  async def test_deploy_all_and_stop_all_are_refused_whole(self, manager, recovering, other):
    for request in (manager.request_deploy_all, manager.request_stop_all):
      with pytest.raises(Conflict, match="^A container of this application can't take this request: an earlier operation"):
        request(recovering.application_id)
    assert manager.task_queue.snapshot()["queued"] == []


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

  async def test_a_cleanup_asks_for_traefiks_networks_so_its_network_goes(self, manager, worker_host, container):
    orphan_placement(worker_host, running=False)
    await manager.cleanup_orphan("w1", ORPHAN)
    assert manager.traefik_networks == ["w1"]

  async def test_a_bare_directory_is_removed(self, manager, worker_host, container, notices):
    directory = orphan_placement(worker_host, running=False)
    await manager.cleanup_orphan("w1", ORPHAN)
    assert not directory.exists()
    assert notices() == [("warning", "Removed a container Sage no longer has from worker w1.", f"{MANAGER}/workers/w1")]

  async def test_a_cleanup_with_nothing_left_stops_nothing_and_sends_no_notice(self, manager, worker_host, container, notices):
    worker_host.flag("down-fails")
    await manager.cleanup_orphan("w1", ORPHAN)
    assert notices() == []

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

  @pytest.mark.parametrize("found", ["unsettled", "unreadable"])
  async def test_old_work_on_an_orphan_is_stopped_and_the_orphan_removed_at_once(self, manager, worker_host, container, found):
    directory = orphan_placement(worker_host, "not a record\n" if found == "unreadable" else TargetRecord(
        authority(SimpleNamespace(id=ORPHAN)), operation=Operation("d9", "deploy", "executing", VERIFIED, input_hash="a" * 64)))
    worker_host.leave_unit(ORPHAN)
    await manager.cleanup_orphan("w1", ORPHAN)
    assert not directory.exists() and worker_host.units() == [] and worker_host.runtime(SimpleNamespace(id=ORPHAN)) is None

  async def test_an_orphan_whose_old_work_cannot_be_stopped_is_left_as_it_is(self, manager, worker_host, container):
    directory = orphan_placement(worker_host)
    worker_host.leave_unit(ORPHAN)
    worker_host.flag("units-stay")
    with pytest.raises(TaskFailed):
      await manager.cleanup_orphan("w1", ORPHAN)
    assert directory.exists() and worker_host.runtime(SimpleNamespace(id=ORPHAN)) == "running d1"

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

  def test_a_request_for_all_replaces_a_containers_request_still_waiting(self, manager, application, containers):
    manager.request_stop(containers[1].id)
    assert len(manager.request_deploy_all(application.id)) == 2
    assert sorted((tuple(entry["key"]), entry["name"]) for entry in manager.task_queue.snapshot()["queued"]) == sorted(
        (("lifecycle", container.id), "deploy") for container in containers)

  async def test_a_request_for_all_is_refused_whole_while_a_containers_operation_runs(self, manager, application, containers):
    release = asyncio.Event()

    async def holds(container_id, deploy):
      await release.wait()

    manager.deploy_container = holds
    manager.request_deploy(containers[1].id)
    manager.task_queue.dispatch_tick()
    await asyncio.sleep(0)
    for request in (manager.request_deploy_all, manager.request_stop_all):
      with pytest.raises(Conflict, match="^A container of this application can't take this request: it already has an operation"):
        request(application.id)
    assert manager.task_queue.snapshot()["queued"] == []
    release.set()
    await manager.run()

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
