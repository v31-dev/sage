import asyncio
import hashlib
import io
import os
import stat
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from services.applications.deploy_input import capture_deploy_config
from services.backups.commands import add_backup
from services.backups.keys import APPLICATIONS_PREFIX, archive_name, parse_volume_archive, taken_at, volume_archive_key
from services.containers.placement import record_path, runtime_name, volume_dir
from services.containers.record import Applied, Baseline, Operation, Settled, TargetRecord
from services.db.models import Backup, Container, Volume
from services.manager.backup_catalog import BackupCatalogMixin
from services.manager.backups import BackupsMixin
from services.manager.queue import SCOPES
from services.s3 import S3
from services.tailscale import Tailscale
from tests.services.manager.test_containers import PAGE, VERIFIED, Lifecycle, Worker, authority, deployed, events, place, worker_record
from utils.archive import decrypt_file
from utils.common import get_env, utcnow
from utils.errors import Conflict
from utils.logging import TaskFailed, task_id
from utils.queue import QueuedTask, QueueState, TaskQueue, next_to_start

CURL = """#!/bin/sh
# A presigned link is a local path: an upload copies the file there and answers 200, or 403 with `upload-fails` naming a
# part of the path; a download copies it from there, or answers 404 without one.
file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --upload-file) file="$2"; shift ;;
    --config) url=$(sed -n 's/^url = "\\(.*\\)"$/\\1/p' "$2"); shift ;;
    --output) out="$2"; shift ;;
  esac
  shift
done
if [ -e "$FAKE_DOCKER/upload-fails" ] && grep -qF "$(cat "$FAKE_DOCKER/upload-fails")" <<EOF
$url
EOF
then echo "AccessDenied" > "$out"; printf 403; exit 0; fi
if [ -z "$file" ]; then
  [ -e "$url" ] && cp "$url" "$out" && printf 200 || printf 404
  exit 0
fi
mkdir -p "$(dirname "$url")" && cp "$file" "$url" && printf 200
"""


class BackupWorker(Worker):
  """The worker of the container tests, with curl replaced and its /tmp in the test's directory."""

  def __init__(self, root: Path):
    super().__init__(root)
    self.tmp = root / "tmp"
    self.tmp.mkdir()
    curl = root / "bin" / "curl"
    curl.write_text(CURL)
    curl.chmod(stat.S_IRWXU)
    self.scripts = []

  def ssh(self, hostname):
    return "reachable" if self.reachable else "unreachable"

  async def exec_command(self, hostname, command, timeout=300):
    return await super().exec_command(hostname, command.replace("/tmp/sage-", f"{self.tmp}/sage-"), timeout)

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    if dst.startswith("/tmp/sage-"):
      self.scripts.append((dst, mode))
    await super().sync_file(hostname, src, dst.replace("/tmp/sage-", f"{self.tmp}/sage-"), keys, formatter, timeout, mode)

  def fail_upload(self, part):
    (self.state / "upload-fails").write_text(part)


class Store:
  """The configured S3 store: a folder its presigned links name, under the base path `base`."""

  def __init__(self, root: Path, identity="s1"):
    self.root, self.identity = root, identity

  def session(self):
    return self

  def get_full_path(self, key):
    return f"base/{key.strip('/')}"

  def create_presigned_upload_url(self, key, expires_in, raw_path=False):
    return str(self.root / key)

  def create_presigned_download_url(self, key, expires_in, raw_path=False, filename=None):
    return str(self.root / key)

  def archive(self, row) -> dict[str, bytes]:
    """The files the archive of `row` holds, by path, decrypted with the encryption key."""
    plain = self.root / "plain.tar.gz"
    decrypt_file(self.root / row.s3_path, plain, get_env("ENCRYPTION_KEY"))
    with tarfile.open(plain) as archive:
      return {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}


class Manager(BackupsMixin, BackupCatalogMixin, Lifecycle):
  def __init__(self):
    super().__init__()
    self.task_queue = TaskQueue(SCOPES, {"app": self.pool, "common": self.pool}, record=lambda task, status: None,
                                notify=self._queue_changed)


@pytest.fixture
def worker_host(register, tmp_path, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", str(tmp_path / "home"))
  return register(Tailscale, BackupWorker(tmp_path))


@pytest.fixture
def store(register, tmp_path):
  store = Store(tmp_path / "store")
  register(S3, SimpleNamespace(session=lambda: store.session()))
  return store


@pytest.fixture
def manager(store, worker_host):
  manager = Manager()
  yield manager
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def in_task():
  token = task_id.set("t1")
  yield
  task_id.reset(token)


@pytest.fixture
def volumes(application):
  return [Volume.create(application=application, name=name, path=f"/{name}") for name in ("data", "logs")]


@pytest.fixture
def container(application, worker, volumes):
  container = Container.create(application=application, worker=worker)
  for volume in volumes:
    directory = Path(volume_dir(config.WORKER_HOME_DIR, container.id, volume.id))
    directory.mkdir(parents=True)
    (directory / f"{volume.name}.txt").write_text(f"{volume.name} of {container.id}")
  return container


def stopped(container, at=VERIFIED):
  """A record of a stop that succeeded after a deploy."""
  return TargetRecord(authority(container), Applied("d0", "a" * 64, at), Settled("s0", "stopped", "succeeded", at),
                      Operation("s0", "stop", "settled", at, 65, at, outcome="succeeded", verified_on_worker_at=at))


def running(container, worker_host):
  place(container, deployed(container))
  worker_host.run(container, "running d0")


def rows():
  return sorted((parse_volume_archive(row.s3_path, "base")[:2], row.storage_id, row.source_volume_name, row.type)
                for row in Backup.select())


def ids(volumes):
  return [volume.id for volume in volumes]


pytestmark = pytest.mark.usefixtures("thread_connections", "in_task")


class TestManualBackup:
  async def test_a_stopped_containers_volumes_are_archived_by_id_and_it_is_committed_stopped(
          self, manager, worker_host, store, container, volumes, notices):
    place(container, stopped(container))
    await manager.backup_container(container.id, ids(volumes), "s1")
    assert rows() == sorted(((volume.id, container.id), "s1", volume.name, "application") for volume in volumes)
    assert notices() == [("success", "Backup of data, logs from Demo / Web on w1 created.", PAGE)]
    for row in Backup.select():
      assert store.archive(row) == {f"{row.source_volume_id}/{row.source_volume_name}.txt":
                                    f"{row.source_volume_name} of {container.id}".encode()}
    record = worker_record(container)
    assert (record.settled.operation_id, record.settled.runtime_goal, record.settled.result) == (record.operation.id, "stopped", "succeeded")
    assert record.applied == stopped(container).applied
    assert [(action, transition) for action, transition, _ in events(container)] == [("backup_stopped", "admitted"),
                                                                                     ("backup_stopped", "succeeded")]
    assert [mode for _, mode in worker_host.scripts] == [0o700, 0o700] and list(worker_host.tmp.iterdir()) == []

  async def test_the_notice_names_only_the_volumes_backed_up(self, manager, worker_host, container, volumes, notices):
    place(container, stopped(container))
    await manager.backup_container(container.id, [volumes[1].id], "s1")
    assert notices() == [("success", "Backup of logs from Demo / Web on w1 created.", PAGE)]

  async def test_a_failure_after_a_stop_is_cleared_by_a_backup(self, manager, worker_host, container, volumes):
    failed = stopped(container)
    place(container, TargetRecord(failed.authority, failed.applied, Settled("d1", "stopped", "failed", VERIFIED, "pull"), failed.operation))
    await manager.backup_container(container.id, ids(volumes), "s1")
    assert worker_record(container).settled.result == "succeeded"

  @pytest.mark.parametrize("setup, refusal", [
      ("running", "running"), ("never deployed", "not_deployed"), ("another store", "store_changed"),
      ("no volumes", "no_volumes")])
  async def test_it_is_refused_with_the_container_untouched(self, manager, worker_host, container, volumes, setup, refusal):
    asked, store_id = ids(volumes), "s1"
    if setup == "running":
      running(container, worker_host)
    elif setup != "never deployed":
      place(container, stopped(container))
    if setup == "another store":
      store_id = "s0"
    if setup == "no volumes":
      asked = ["0123456789abcdef"]
    before = worker_record(container)
    with pytest.raises(TaskFailed):
      await manager.backup_container(container.id, asked, store_id)
    assert worker_record(container) == before and Backup.select().count() == 0
    assert events(container) == [("backup_stopped", "rejected", {"refusal": refusal})]

  async def test_a_failed_upload_keeps_the_other_archive_and_leaves_the_container_in_error(
          self, manager, worker_host, container, volumes):
    place(container, stopped(container))
    worker_host.fail_upload(volumes[0].id)
    with pytest.raises(TaskFailed):
      await manager.backup_container(container.id, ids(volumes), "s1")
    assert [name for _, _, name, _ in rows()] == ["logs"]
    settled = worker_record(container).settled
    assert (settled.runtime_goal, settled.result) == ("stopped", "failed") and "volume data" in settled.reason
    assert events(container)[-1][:2] == ("backup_stopped", "failed")


class TestManualRequests:
  @pytest.fixture
  def others(self, application, volumes):
    from services.db.models import Worker as Host
    return Container.create(application=application, worker=Host.create(hostname="w2", subdomain="blue"))

  async def held(self, container, record):
    from services.containers.targets import Containers
    place(container, record)
    await Containers().read(container)

  async def test_a_backup_queues_one_task_per_container_in_its_lifecycle_slot_holding_the_store(
          self, manager, worker_host, container, others, volumes):
    for each in (container, others):
      await self.held(each, stopped(each))
    manager.request_backup(container.application_id, ids(volumes))
    assert sorted((tuple(task["key"]), task["name"], task["scopes"], task["params"]["storage_id"])
                  for task in manager.task_queue.snapshot()["queued"]) == sorted(
        (("lifecycle", each.id), "backup_stopped",
         sorted({f"worker:{each.worker_id}:container:{each.id}", f"backup:store:s1:write:{each.id}"}), "s1")
        for each in (container, others))

  async def test_it_is_refused_while_a_container_runs(self, manager, worker_host, container, others, volumes):
    await self.held(container, stopped(container))
    await self.held(others, deployed(others))
    with pytest.raises(Conflict, match="it runs"):
      manager.request_backup(container.application_id, ids(volumes))
    assert manager.task_queue.snapshot()["queued"] == []

  def test_it_is_refused_for_volumes_of_another_application(self, manager, container, volumes):
    with pytest.raises(ValueError):
      manager.request_backup(container.application_id, ["0123456789abcdef"])

  def test_it_is_refused_without_a_store(self, manager, container, volumes, register):
    def unset():
      raise Conflict("S3 is not configured.")
    register(S3, SimpleNamespace(session=unset))
    with pytest.raises(Conflict, match="S3 is not configured"):
      manager.request_backup(container.application_id, ids(volumes))


class TestAutomaticBackup:
  async def test_a_running_container_is_stopped_as_it_is_archived_and_started_again_the_same(
          self, manager, worker_host, store, container, volumes):
    running(container, worker_host)
    log = []
    original = worker_host.exec_command

    async def exec_command(hostname, command, timeout=300):
      log.append(command.split()[4] if command.startswith("docker compose -p") else "archive" if "sage-backup-" in command else None)
      return await original(hostname, command, timeout)

    worker_host.exec_command = exec_command
    await manager.backup_container_preserving(container.id, ids(volumes), "s1")
    assert [step for step in log if step] == ["ps", "stop", "archive", "archive", "start", "ps"]
    record = worker_record(container)
    assert (record.operation.contract, record.operation.phase, record.operation.outcome) == ("backup_preserve", "settled", "succeeded")
    assert record.operation.baseline == Baseline("d0", "running", "d0", (f"id-{runtime_name(container.id)}",))
    assert (record.applied, record.settled) == (deployed(container).applied, deployed(container).settled)
    assert worker_host.runtime(container) == "running d0" and len(rows()) == 2
    assert [transition for _, transition, _ in events(container)] == ["admitted", "succeeded"]

  async def test_a_stopped_container_is_archived_and_left_stopped(self, manager, worker_host, container, volumes):
    place(container, stopped(container))
    await manager.backup_container_preserving(container.id, ids(volumes), "s1")
    record = worker_record(container)
    assert record.operation.baseline == Baseline("s0", "stopped", None, ()) and record.operation.outcome == "succeeded"
    assert worker_host.runtime(container) is None and record.settled == stopped(container).settled

  async def test_a_failed_archive_is_the_tasks_failure_and_the_runtime_comes_back(
          self, manager, worker_host, container, volumes, notices):
    running(container, worker_host)
    worker_host.fail_upload(volumes[1].id)
    with pytest.raises(TaskFailed):
      await manager.backup_container_preserving(container.id, ids(volumes), "s1")
    [(level, content, link)] = notices()
    assert (level, content.startswith("Automatic backup of Demo / Web on w1 failed: volume logs"), link) == ("error", True, PAGE)
    record = worker_record(container)
    assert (record.operation.phase, record.operation.outcome) == ("settled", "failed") and "volume logs" in record.operation.reason
    assert record.settled == deployed(container).settled and worker_host.runtime(container) == "running d0"
    assert [name for _, _, name, _ in rows()] == ["data"]
    assert events(container)[-1][:2] == ("backup_preserve", "failed")

  async def test_a_failed_stop_still_brings_the_runtime_back(self, manager, worker_host, container, volumes):
    running(container, worker_host)
    worker_host.flag("stop-fails")
    with pytest.raises(TaskFailed):
      await manager.backup_container_preserving(container.id, ids(volumes), "s1")
    assert worker_record(container).operation.outcome == "failed" and worker_host.runtime(container) == "running d0"
    assert rows() == []

  @pytest.mark.parametrize("change", ["start-fails", "replaced", "another deployment"])
  async def test_a_runtime_that_does_not_come_back_the_same_is_stopped_and_settled_failed(
          self, manager, worker_host, container, volumes, change):
    running(container, worker_host)
    original = manager._archive

    async def archive(*args):
      if change == "another deployment":
        worker_host.run(container, "exited d9")
      else:
        worker_host.flag(change)
      return await original(*args)

    manager._archive = archive
    with pytest.raises(TaskFailed):
      await manager.backup_container_preserving(container.id, ids(volumes), "s1")
    settled = worker_record(container).settled
    assert (settled.runtime_goal, settled.result) == ("stopped", "failed") and "started again" in settled.reason
    assert worker_host.runtime(container) is None and len(rows()) == 2

  @pytest.mark.parametrize("setup, refusal", [
      ("failed", "failed"), ("exited", "unexpected_runtime"), ("never deployed", "not_deployed"), ("another store", "store_changed")])
  async def test_it_is_refused_as_a_failed_task_with_the_container_untouched(
          self, manager, worker_host, container, volumes, setup, refusal):
    store_id = "s0" if setup == "another store" else "s1"
    if setup == "failed":
      record = stopped(container)
      place(container, TargetRecord(record.authority, record.applied, Settled("d1", "stopped", "failed", VERIFIED, "pull"),
                                    record.operation))
    elif setup != "never deployed":
      running(container, worker_host)
    if setup == "exited":
      worker_host.run(container, "exited d0")
    before = (worker_record(container), worker_host.runtime(container))
    with pytest.raises(TaskFailed):
      await manager.backup_container_preserving(container.id, ids(volumes), store_id)
    assert (worker_record(container), worker_host.runtime(container)) == before and rows() == []
    assert events(container) == [("backup_preserve", "rejected", {"refusal": refusal})]


class TestEdges:
  @pytest.mark.parametrize("task", ["backup_container", "backup_container_preserving"])
  async def test_a_container_deleted_before_its_backup_runs_is_a_quiet_no_op(self, manager, worker_host, container, volumes, task):
    Container.delete().where(Container.id == container.id).execute()
    await getattr(manager, task)(container.id, ids(volumes), "s1")
    assert rows() == []

  async def test_an_automatic_backup_whose_end_is_not_recorded_fails_with_the_runtime_back(
          self, manager, worker_host, container, volumes, monkeypatch):
    from services.containers.targets import Containers
    running(container, worker_host)
    original, writes = Containers().write, []

    async def write(container, record):
      writes.append(record)
      if len(writes) == 2:
        raise Exception("Syncing file to w1 timed out")
      await original(container, record)

    monkeypatch.setattr(Containers(), "write", write)
    with pytest.raises(TaskFailed):
      await manager.backup_container_preserving(container.id, ids(volumes), "s1")
    assert worker_record(container).operation.phase == "executing" and worker_host.runtime(container) == "running d0"
    assert events(container)[-1] == ("backup_preserve", "failed", {"error": "its end could not be recorded on the worker: "
                                                                            "Syncing file to w1 timed out"})


class TestAutomaticRequests:
  @pytest.fixture
  def at(self):
    return datetime(2026, 1, 1, 3, 0, 20)

  def queued(self, manager):
    return [(tuple(task["key"]), task["name"], task["params"]["volume_ids"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]]

  def test_each_container_of_an_application_with_volumes_due_gets_its_own_automatic_backup(self, manager, container, volumes, at):
    Volume.update(backup_cron="0 3 * * *").where(Volume.id == volumes[0].id).execute()
    Volume.update(backup_cron="0 4 * * *").where(Volume.id == volumes[1].id).execute()
    manager.request_automatic_backups(at)
    assert self.queued(manager) == [(("backup_preserve", container.id, volumes[0].id), "backup_preserve", [volumes[0].id], False)]
    [task] = manager.task_queue.snapshot()["queued"]
    assert task["scopes"] == sorted({f"worker:{container.worker_id}:container:{container.id}", f"backup:store:s1:write:{container.id}"})

  def test_other_volumes_due_while_a_backup_waits_get_their_own_and_the_same_ones_do_not(self, manager, container, volumes, at):
    Volume.update(backup_cron="* * * * *").where(Volume.id == volumes[0].id).execute()
    Volume.update(backup_cron="1 3 * * *").where(Volume.id == volumes[1].id).execute()
    manager.request_stop(container.id)
    for minute in range(3):
      manager.request_automatic_backups(at + timedelta(minutes=minute))
    assert [(task["name"], task["params"].get("volume_ids")) for task in manager.task_queue.snapshot()["queued"]] == [
        ("stop", None), ("backup_preserve", [volumes[0].id]), ("backup_preserve", sorted(ids(volumes)))]

  def test_a_volume_backed_up_this_minute_is_not_due_again(self, manager, container, volumes, at):
    Volume.update(backup_cron="0 3 * * *").where(Volume.id == volumes[0].id).execute()
    Backup.create(s3_path="k", storage_id="s1", type="application", source_volume_id=volumes[0].id, source_volume_name="data",
                  application=container.application_id, created_at=at.replace(second=5))
    manager.request_automatic_backups(at)
    assert self.queued(manager) == []

  def test_nothing_is_queued_without_a_store(self, manager, container, volumes, at, register):
    def unset():
      raise Conflict("S3 is not configured.")
    register(S3, SimpleNamespace(session=unset))
    Volume.update(backup_cron="0 3 * * *").execute()
    manager.request_automatic_backups(at)
    assert self.queued(manager) == []

  def test_an_invalid_schedule_is_logged_and_skipped(self, manager, container, volumes, at, caplog):
    Volume.update(backup_cron="not a cron").where(Volume.id == volumes[0].id).execute()
    Volume.update(backup_cron="0 3 * * *").where(Volume.id == volumes[1].id).execute()
    manager.request_automatic_backups(at)
    assert [ids for _, _, ids, _ in self.queued(manager)] == [[volumes[1].id]]
    assert "invalid backup schedule" in caplog.text

  async def test_a_user_request_during_a_running_automatic_backup_waits_for_it_and_is_not_refused(
          self, manager, worker_host, container, volumes, at):
    running(container, worker_host)
    Volume.update(backup_cron="0 3 * * *").execute()
    release = asyncio.Event()
    original = manager._archive

    async def archive(*args):
      await release.wait()
      return await original(*args)

    manager._archive = archive
    manager.request_automatic_backups(at)
    manager.task_queue.dispatch_tick()
    while not manager.task_queue.snapshot()["running"]:
      await asyncio.sleep(0)
    task_id = manager.request_stop(container.id)
    manager.task_queue.dispatch_tick()
    assert [task["task_id"] for task in manager.task_queue.snapshot()["queued"]] == [task_id]
    release.set()
    for _ in range(500):
      if not (manager.task_queue.snapshot()["running"] or manager.task_queue.snapshot()["queued"]):
        break
      manager.task_queue.dispatch_tick()
      await asyncio.sleep(0.01)
    assert worker_record(container).settled.runtime_goal == "stopped" and len(rows()) == 2


class TestInterruptedAutomaticBackup:
  def interrupted(self, container, worker_host, generation=None):
    """A running container whose automatic backup stopped it and never settled."""
    record = deployed(container)
    baseline = Baseline("d0", "running", "d0", (f"id-{runtime_name(container.id)}",))
    place(container, TargetRecord(authority(container, generation), record.applied, record.settled,
                                  Operation("b1", "backup_preserve", "executing", VERIFIED, 905, baseline=baseline)))
    worker_host.run(container, "exited d0", labelled=authority(container, generation))

  async def test_it_is_stopped_as_it_is_then_brought_back_to_its_baseline(self, manager, worker_host, container):
    self.interrupted(container, worker_host)
    await manager.recover_container(container.id, "b1", final=False)
    assert worker_host.runtime(container) == "exited d0" and worker_record(container).operation.phase == "recovering"
    await manager.recover_container(container.id, "b1", final=True)
    record = worker_record(container)
    assert (record.operation.phase, record.operation.outcome, record.operation.recovery.result) == ("settled", "interrupted", "verified")
    assert record.settled == deployed(container).settled and worker_host.runtime(container) == "running d0"
    assert [transition for _, transition, _ in events(container)] == ["interrupted", "recovered"]

  async def test_one_that_does_not_come_back_is_stopped_and_settled_failed(self, manager, worker_host, container):
    self.interrupted(container, worker_host)
    worker_host.flag("start-fails")
    await manager.recover_container(container.id, "b1", final=True)
    settled = worker_record(container).settled
    assert (settled.runtime_goal, settled.result) == ("stopped", "failed") and "started again" in settled.reason
    assert worker_host.runtime(container) is None

  async def test_one_from_before_a_platform_restore_is_not_brought_back(self, manager, worker_host, container):
    self.interrupted(container, worker_host, generation="0123456789abcdef")
    await manager.recover_container(container.id, "b1", final=True)
    settled = worker_record(container).settled
    assert (settled.runtime_goal, settled.result) == ("stopped", "failed") and worker_host.runtime(container) is None
    assert worker_record(container).authority == authority(container)


class TestRestore:
  @pytest.fixture
  def other(self, application):
    """A container of the application on another worker, never deployed."""
    from services.db.models import Worker as Host
    return Container.create(application=application, worker=Host.create(hostname="w2", subdomain="blue"))

  @pytest.fixture
  async def archived(self, manager, worker_host, container, volumes):
    """The volumes of `container` backed up, by volume name."""
    place(container, stopped(container))
    await manager.backup_container(container.id, ids(volumes), "s1")
    return {row.source_volume_name: row for row in Backup.select()}

  def files(self, container, volume):
    directory = Path(volume_dir(config.WORKER_HOME_DIR, container.id, volume.id))
    return {path.name: path.read_text() for path in directory.iterdir()} if directory.exists() else None

  async def test_a_restore_rejects_an_archive_replaced_by_a_backup_after_retention(
          self, manager, worker_host, store, container, volumes, monkeypatch, thread_connections):
    from services.containers.targets import Containers
    place(container, stopped(container))
    await Containers().read(container)
    before = worker_record(container)
    selected = add_backup(store.get_full_path(volume_archive_key(
        volumes[0].id, container.id, archive_name(utcnow() - timedelta(days=config.RETENTION_DAYS + 1)))),
        store.identity, volumes[0].id, volumes[0].name, container.application_id)
    volumes[1].backup_cron = "* * * * *"
    volumes[1].save()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delete_key(prefix, filter=None, raw_path=False):
      if prefix != APPLICATIONS_PREFIX:
        return []
      entered.set()
      await release.wait()
      return [selected.s3_path] if filter(selected.s3_path) else []

    monkeypatch.setattr(store, "delete_key", delete_key, raising=False)
    manager.request_backup_retention()
    manager.task_queue.dispatch_tick()
    try:
      await asyncio.wait_for(entered.wait(), 2)
      manager.request_automatic_backups(utcnow())
      manager.request_restore(container.id, selected.id, volumes[0].id)
    finally:
      release.set()
      await manager.run()
    assert Backup.get_or_none(Backup.id == selected.id) is None
    replacement = Backup.get()
    assert replacement.s3_path != selected.s3_path and replacement.source_volume_id == volumes[1].id
    assert replacement.id != selected.id
    assert self.files(container, volumes[0]) == {"data.txt": f"data of {container.id}"}
    record = worker_record(container)
    assert record.operation.contract == "backup_preserve" and record.settled == before.settled and record.applied == before.applied
    assert [event for event in events(container) if event[0] == "restore_stopped"] == [
        ("restore_stopped", "rejected", {"refusal": "no_backup"})]

  async def test_a_backup_is_restored_into_a_container_on_another_worker_never_deployed(
          self, manager, worker_host, container, other, volumes, archived, notices):
    await manager.restore_container(other.id, archived["data"].id, volumes[0].id, "s1")
    assert self.files(other, volumes[0]) == {"data.txt": f"data of {container.id}"}
    assert notices()[-1] == ("success", f"Volume data of Demo / Web on w2 restored from the backup of data taken at "
                             f"{taken_at(archived['data'].s3_path)}.", PAGE)
    record = worker_record(other)
    assert (record.applied, record.settled.runtime_goal, record.settled.result) == (None, "stopped", "succeeded")
    assert events(other)[-1] == ("restore_stopped", "succeeded", {"volume": "data", "backup_of": "data"})

  async def test_a_restore_keeps_its_source_when_another_stores_discovery_removes_its_row(
          self, manager, worker_host, store, container, other, volumes, archived, monkeypatch, register, thread_connections):
    entered, release = asyncio.Event(), asyncio.Event()
    sync_file = worker_host.sync_file

    async def gated_sync(hostname, src, dst, keys=None, **kwargs):
      if dst == record_path(config.WORKER_HOME_DIR, other.id) and not entered.is_set():
        entered.set()
        await release.wait()
      return await sync_file(hostname, src, dst, keys, **kwargs)

    async def get_keys(prefix):
      return []

    monkeypatch.setattr(worker_host, "sync_file", gated_sync)
    manager.request_restore(other.id, archived["data"].id, volumes[0].id)
    manager.task_queue.dispatch_tick()
    try:
      await asyncio.wait_for(entered.wait(), 2)
      register(S3, SimpleNamespace(session=lambda: SimpleNamespace(identity="s2", target=SimpleNamespace(path="elsewhere"), get_keys=get_keys)))
      manager.request_backup_discovery()
      manager.task_queue.dispatch_tick()
      async with asyncio.timeout(2):
        while manager.has_task(("discover_backups",)):
          await asyncio.sleep(0)
      assert Backup.select().count() == 0
    finally:
      release.set()
      await manager.run()
    assert self.files(other, volumes[0]) == {"data.txt": f"data of {container.id}"}
    assert worker_record(other).settled.result == "succeeded"
    assert events(other)[-1] == ("restore_stopped", "succeeded", {"volume": "data", "backup_of": "data"})

  async def test_it_is_restored_into_another_volume_of_the_application_whose_files_it_replaces(
          self, manager, worker_host, container, volumes, archived, notices):
    await manager.restore_container(container.id, archived["data"].id, volumes[1].id, "s1")
    assert self.files(container, volumes[1]) == {"data.txt": f"data of {container.id}"}
    assert self.files(container, volumes[0]) == {"data.txt": f"data of {container.id}"}
    assert notices()[-1][1].startswith("Volume logs of Demo / Web on w1 restored from the backup of data taken at ")

  async def test_a_deploy_after_it_keeps_the_restored_data(self, manager, worker_host, container, other, volumes, archived):
    await manager.restore_container(other.id, archived["logs"].id, volumes[1].id, "s1")
    await manager.deploy_container(other.id, capture_deploy_config(other.application_id))
    assert worker_host.runtime(other).startswith("running") and self.files(other, volumes[1]) == {"logs.txt": f"logs of {container.id}"}

  @pytest.mark.parametrize("setup, refusal", [
      ("running", "running"), ("runs though stopped", "running"), ("another store", "store_changed"),
      ("backup in another store", "store_changed"), ("backup gone", "no_backup"), ("volume gone", "no_volumes")])
  async def test_it_is_refused_with_the_container_untouched(self, manager, worker_host, container, volumes, archived, setup, refusal):
    store_id, backup = "s1", archived["data"]
    if setup == "running":
      running(container, worker_host)
    elif setup == "runs though stopped":
      worker_host.run(container, "running d0")
    elif setup == "another store":
      store_id = "s0"
    elif setup == "backup in another store":
      Backup.update(storage_id="s0").where(Backup.id == backup.id).execute()
    elif setup == "backup gone":
      Backup.delete().where(Backup.id == backup.id).execute()
    else:
      Volume.delete().where(Volume.id == volumes[1].id).execute()
    before = worker_record(container)
    with pytest.raises(TaskFailed):
      await manager.restore_container(container.id, backup.id, volumes[1].id, store_id)
    assert worker_record(container) == before and self.files(container, volumes[1]) in (None, {"logs.txt": f"logs of {container.id}"})
    assert events(container)[-1] == ("restore_stopped", "rejected", {"refusal": refusal})

  @pytest.mark.parametrize("damage", ["altered", "missing"])
  async def test_a_failed_restore_leaves_the_container_in_error_and_a_deploy_allowed(
          self, manager, worker_host, store, container, other, volumes, archived, damage):
    path = store.root / archived["data"].s3_path
    if damage == "altered":
      path.write_bytes(path.read_bytes()[:-1] + b"\0")
    else:
      path.unlink()
    with pytest.raises(TaskFailed):
      await manager.restore_container(other.id, archived["data"].id, volumes[0].id, "s1")
    settled = worker_record(other).settled
    assert (settled.runtime_goal, settled.result) == ("stopped", "failed") and self.files(other, volumes[0]) is None
    await manager.deploy_container(other.id, capture_deploy_config(other.application_id))
    assert worker_record(other).settled.result == "succeeded"


class TestRestoreRequests:
  @pytest.fixture
  def backup(self, container, volumes):
    return Backup.create(s3_path="base/backups/applications/x", storage_id="s1", type="application", source_volume_id=volumes[0].id,
                         source_volume_name="data", application=container.application_id)

  async def test_a_restore_takes_the_lifecycle_slot_holding_the_backup_and_names_both_volumes(
          self, manager, worker_host, container, volumes, backup):
    from services.containers.targets import Containers
    place(container, stopped(container))
    await Containers().read(container)
    manager.request_restore(container.id, backup.id, volumes[1].id)
    [task] = manager.task_queue.snapshot()["queued"]
    assert (tuple(task["key"]), task["name"], task["params"]) == (
        ("lifecycle", container.id), "restore_stopped", {"container_id": container.id, "backup_id": backup.id, "volume_id": volumes[1].id,
                                                         "storage_id": "s1"})
    part = hashlib.sha256(backup.s3_path.encode()).hexdigest()[:16]
    assert task["scopes"] == sorted({f"worker:{container.worker_id}:container:{container.id}", f"backup:store:s1:archive:{part}"})
    assert (task["labels"]["Volume"], task["labels"]["Backup of"]) == ("logs", "data")

  async def test_a_restore_and_the_deletion_of_its_archive_take_turns_and_other_archives_do_not_wait(
          self, manager, worker_host, container, volumes, backup):
    from services.containers.targets import Containers
    place(container, stopped(container))
    await Containers().read(container)
    other = Backup.create(s3_path="base/backups/applications/y", storage_id="s1", type="application",
                          source_volume_id=volumes[0].id, source_volume_name="data", application=container.application_id)
    manager.request_restore(container.id, backup.id, volumes[0].id)
    manager.request_backup_deletion(backup.id)
    manager.request_backup_deletion(other.id)
    restore, own, elsewhere = [QueuedTask(name=task["name"], key=tuple(task["key"]), scopes=frozenset(task["scopes"]),
                                          task=None, executor=task["executor"]) for task in manager.task_queue.snapshot()["queued"]]
    assert (restore.name, own.key, elsewhere.key) == (
        "restore_stopped", ("delete_archive", "s1", backup.s3_path), ("delete_archive", "s1", other.s3_path))
    assert next_to_start(QueueState.RUNNING, [own, elsewhere], [restore]) == [elsewhere]
    assert next_to_start(QueueState.RUNNING, [restore], [own]) == []

  async def test_it_is_refused_while_the_container_runs(self, manager, worker_host, container, volumes, backup):
    from services.containers.targets import Containers
    running(container, worker_host)
    await Containers().read(container)
    with pytest.raises(Conflict, match="it runs"):
      manager.request_restore(container.id, backup.id, volumes[0].id)

  def test_a_backup_in_another_store_is_refused(self, manager, container, volumes, backup):
    Backup.update(storage_id="s0").where(Backup.id == backup.id).execute()
    with pytest.raises(Conflict, match="not in the configured store"):
      manager.request_restore(container.id, backup.id, volumes[0].id)

  def test_a_backup_or_volume_of_another_application_is_refused(self, manager, container, volumes, backup, project):
    from services.db.models import Application
    elsewhere = Application.create(project=project, name="api", label="API", image="nginx")
    foreign = Volume.create(application=elsewhere, name="data", path="/data")
    with pytest.raises(ValueError):
      manager.request_restore(container.id, backup.id, foreign.id)
    Backup.update(application=elsewhere).where(Backup.id == backup.id).execute()
    with pytest.raises(ValueError):
      manager.request_restore(container.id, backup.id, volumes[0].id)

  async def test_a_container_deleted_before_its_restore_runs_is_a_quiet_no_op(self, manager, worker_host, container, volumes, backup):
    Container.delete().where(Container.id == container.id).execute()
    await manager.restore_container(container.id, backup.id, volumes[0].id, "s1")
    assert events(container) == []

  def test_a_backup_not_listed_is_not_found(self, manager, container, volumes):
    with pytest.raises(LookupError):
      manager.request_restore(container.id, "missing", volumes[0].id)
