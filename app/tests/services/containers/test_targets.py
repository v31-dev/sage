import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import config
from services.containers.placement import record_path
from services.containers.record import Authority, Operation, TargetRead, TargetRecord, decode_record
from services.containers.targets import Containers
from services.db.models import Application, Container
from services.tailscale import Tailscale
from services.updates import UpdateSource

AT = datetime(2026, 1, 1, 12)


class Worker:
  """A worker that runs commands in a local shell and puts synced files under the test's directory, as SSH and SFTP
  would on the worker."""

  def __init__(self):
    self.reachable = True
    self.writes_fail = False

  async def exec_command(self, hostname, command, timeout=300):
    if not self.reachable:
      raise Exception(f"Error connecting to {hostname}: no route")
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
    if result.returncode:
      raise Exception(f"Error executing command on {hostname}: exit code {result.returncode}")
    return result.returncode, result.stdout.splitlines()

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    if not self.reachable or self.writes_fail:
      raise Exception(f"Syncing file to {hostname} timed out after {timeout}s: {dst}")
    content = Path(src).read_text()
    for key, value in (keys or {}).items():
      content = content.replace("${" + key + "}", str(value))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(content)
    Path(dst).chmod(mode)


@pytest.fixture
def worker_host(register, tmp_path, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", str(tmp_path / "home"))
  return register(Tailscale, Worker())


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


def record_of(container, operation="d1"):
  return TargetRecord(Authority("0123456789abcdef", "1111111111111111", container.id),
                      operation=Operation(operation, "stop", "executing", AT, 65, task_ref="t1"))


PUBLISHED = [(UpdateSource.APPLICATION, "demo/web"), (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.DOMAINS, None)]


class TestReads:
  def test_a_container_is_unobserved_until_its_record_is_read(self, container):
    assert Containers().target(container.id) == TargetRead("unobserved")

  async def test_a_container_without_a_record_reads_absent(self, worker_host, container):
    assert await Containers().read(container) == TargetRead("absent")
    assert Containers().target(container.id) == TargetRead("absent")

  async def test_a_written_record_is_what_the_worker_holds_and_reads_back(self, worker_host, container):
    record = record_of(container)
    await Containers().write(container, record)
    assert decode_record(Path(record_path(config.WORKER_HOME_DIR, container.id)).read_text()) == record
    assert Containers().target(container.id) == TargetRead("present", record)
    assert await Containers().read(container) == TargetRead("present", record)

  async def test_a_file_that_is_not_its_record_reads_invalid(self, worker_host, container):
    path = Path(record_path(config.WORKER_HOME_DIR, container.id))
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    assert await Containers().read(container) == TargetRead("invalid")

  async def test_another_containers_record_is_not_its_own(self, worker_host, container, project, worker):
    other = Container.create(application=Application.create(project=project, name="api", label="API", image="nginx"),
                             worker=worker)
    await Containers().write(other, record_of(other))
    assert await Containers().read(container) == TargetRead("absent")

  async def test_a_worker_that_cannot_be_asked_reads_unreachable_with_the_last_record(self, worker_host, container):
    record = record_of(container)
    await Containers().write(container, record)
    worker_host.reachable = False
    assert await Containers().read(container) == TargetRead("unreachable", record)
    assert await Containers().read(container) == TargetRead("unreachable", record)


class TestWrites:
  async def test_a_write_that_may_not_have_landed_raises_and_keeps_the_last_read(self, worker_host, container):
    await Containers().read(container)
    worker_host.writes_fail = True
    with pytest.raises(Exception, match="timed out"):
      await Containers().write(container, record_of(container))
    assert Containers().target(container.id) == TargetRead("absent")

  async def test_a_write_replaces_the_record_whole(self, worker_host, container):
    await Containers().write(container, record_of(container, "d1"))
    await Containers().write(container, record_of(container, "d2"))
    assert await Containers().read(container) == TargetRead("present", record_of(container, "d2"))


class TestPublication:
  async def test_the_pages_showing_the_container_are_told_when_its_read_changes_and_only_then(
          self, worker_host, container, signals):
    await Containers().read(container)
    assert signals == PUBLISHED
    await Containers().read(container)
    assert signals == PUBLISHED
    await Containers().write(container, record_of(container))
    assert signals == PUBLISHED * 2
    await Containers().read(container)
    assert signals == PUBLISHED * 2
    worker_host.reachable = False
    await Containers().read(container)
    assert signals == PUBLISHED * 3

  async def test_a_write_that_failed_tells_nobody(self, worker_host, container, signals):
    worker_host.writes_fail = True
    with pytest.raises(Exception):
      await Containers().write(container, record_of(container))
    assert signals == []
