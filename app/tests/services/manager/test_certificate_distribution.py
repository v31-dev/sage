import asyncio
import json
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import config
from services.certs import TEMPLATE_DIGEST, Certs
from services.db.models import Worker
from services.manager.certificates import CertificatesMixin
from services.manager.queue import SCOPES, QueueMixin
from services.tailscale import Tailscale
from services.updates import Updates
from services.workers.state import Workers
from tests.services.test_certs import write_certificate
from utils.executor import active_executor
from utils.logging import TaskFailed
from utils.queue import TaskQueue

HOME = "/opt/sage"

MANAGER = "https://manager.core.example.test"


class LocalWorkers:
  """Workers whose Sage home is a folder each under `root`; their commands run in a local shell, and every file
  written or command run is logged in order. `reachable` are those whose SSH server answers; `fails`, a part of a
  destination, makes that write fail."""

  def __init__(self, root):
    self.root, self.log, self.reachable, self.fails = root, [], set(), None

  def ssh(self, hostname):
    return "reachable" if hostname in self.reachable else "unreachable"

  def home(self, hostname):
    return self.root / hostname

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    if self.fails and self.fails in dst:
      raise Exception(f"Error writing {dst} on {hostname}")
    content = Path(src).read_bytes()
    for key, value in (keys or {}).items():
      content = content.replace(("${" + key + "}").encode(), str(value).encode())
    path = self.home(hostname) / Path(dst).relative_to(HOME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    self.log.append((hostname, "write", dst))

  async def exec_command(self, hostname, command, timeout=300):
    self.home(hostname).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command.replace(HOME, str(self.home(hostname))), shell=True, capture_output=True, text=True)
    self.log.append((hostname, "run", command))
    return result.returncode, result.stdout.splitlines()

  def writes(self, hostname):
    return [entry for entry in self.log if entry[0] == hostname and (entry[1] == "write" or entry[2].startswith("rm "))]


class Manager(CertificatesMixin, QueueMixin):
  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=2)
    self.queue_facts = []
    self.checks = 0
    self.worker_state = SimpleNamespace(check=self.count_check)
    self.task_queue = TaskQueue(SCOPES, {"platform": self.pool, "app": self.pool}, record=lambda task, status: None,
                                notify=self._queue_changed)

  def count_check(self):
    self.checks += 1


@pytest.fixture
def certs(data_dir, clock, register):
  clock.set(datetime.now(timezone.utc).replace(tzinfo=None))
  register(Updates, SimpleNamespace(changed=lambda source, key=None: None, publish=lambda feed, rows, key=None: None))
  certs = Certs()
  certs.set_workers({"w1": "red", "w2": "blue"})
  for certificate in certs.inventory().values():
    write_certificate(certificate.fullchain_path.parent, certificate.names)
    certs.reload(certificate)
  return certs


@pytest.fixture
def hosts(register, tmp_path):
  hosts = register(Tailscale, LocalWorkers(tmp_path / "remote"))
  hosts.reachable = {"w1", "w2"}
  return hosts


@pytest.fixture
def manager(hosts, monkeypatch):
  monkeypatch.setattr(config, "WORKER_HOME_DIR", HOME)
  manager = Manager()
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def workers(db, certs):
  for hostname, subdomain in (("w1", "red"), ("w2", "blue")):
    Worker.create(hostname=hostname, subdomain=subdomain)
    Workers().record_setup(hostname, "succeeded")


def managed(hosts, hostname):
  return hosts.home(hostname) / "traefik/certs/managed"


def listed(hosts, hostname):
  return yaml.safe_load((hosts.home(hostname) / "traefik/dynamic/certs.yml").read_text())["tls"]["certificates"]


def state(hostname, subdomain):
  return Certs().worker_state(hostname, subdomain, Workers().certificates(hostname), TEMPLATE_DIGEST)


def queued(manager):
  return [(tuple(task["key"]), task["scopes"], task["quiet"]) for task in manager.task_queue.snapshot()["queued"]]


class TestRequests:
  def test_a_copy_holds_the_workers_share_of_the_certificates_and_its_files_and_a_forced_one_is_its_own(self, manager):
    manager.request_certificate_distribution("w1")
    manager.request_certificate_distribution("w1", force=True)
    assert queued(manager) == [
        (("distribute_certificates", "w1"), ["certificate:worker:w1", "worker:w1:tls"], True),
        (("distribute_certificates", "w1", "force"), ["certificate:worker:w1", "worker:w1:tls"], False)]

  async def test_a_renewal_and_the_workers_host_wide_work_hold_it_back_and_the_other_workers_go_on(
          self, manager, hosts, workers):
    releases = {name: asyncio.Event() for name in ("renew_certificates", "converge_worker")}

    def held(name):
      async def wait():
        await releases[name].wait()
      return wait

    for name, scopes in (("renew_certificates", {"certificate"}), ("converge_worker", {"worker:w2"})):
      manager.add_task(task=held(name), key=(name,), scopes=scopes, executor="app")
    manager.task_queue.dispatch_tick()
    for hostname in ("w1", "w2"):
      manager.request_certificate_distribution(hostname)
    manager.task_queue.dispatch_tick()
    assert [tuple(task["key"]) for task in manager.task_queue.snapshot()["queued"]] == [
        ("distribute_certificates", "w1"), ("distribute_certificates", "w2")]
    releases["renew_certificates"].set()
    for _ in range(500):
      if state("w1", "red")["worker"]["synced"]:
        break
      manager.task_queue.dispatch_tick()
      await asyncio.sleep(0.01)
    assert [tuple(task["key"]) for task in manager.task_queue.snapshot()["queued"]] == [("distribute_certificates", "w2")]
    releases["converge_worker"].set()
    for _ in range(500):
      if not (manager.task_queue.snapshot()["running"] or manager.task_queue.snapshot()["queued"]):
        break
      manager.task_queue.dispatch_tick()
      await asyncio.sleep(0.01)
    assert state("w1", "red")["worker"]["synced"] and state("w2", "blue")["worker"]["synced"]

  async def test_a_renewal_has_every_worker_reachable_and_set_up_get_its_copy(self, manager, hosts, workers, monkeypatch):
    Worker.create(hostname="w3", subdomain="green")
    Workers().record_setup("w3", "succeeded")
    Worker.create(hostname="w4", subdomain="gold")
    hosts.reachable.add("w4")
    monkeypatch.setattr(Certs(), "ensure", lambda certificate: False)
    await manager.renew_certificates()
    assert [key for key, _, _ in queued(manager)] == [("distribute_certificates", "w1"), ("distribute_certificates", "w2")]
    assert manager.checks == 1


class TestDistribution:
  async def test_only_its_own_and_the_shared_pairs_are_copied_the_key_private_and_the_stamp_last(self, manager, hosts, workers):
    await manager.distribute_certificates("w1")
    files = managed(hosts, "w1")
    assert {path.read_bytes() for path in files.glob("*.key")} == {
        certificate.key_path.read_bytes() for certificate in Certs().for_worker("w1").values()}
    assert len(list(files.iterdir())) == 4
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files.glob("*.key"))
    assert sorted(entry["certFile"].rsplit("/", 1)[1] for entry in listed(hosts, "w1")) == sorted(
        path.name for path in files.glob("*.pem"))
    assert hosts.writes("w1")[-1] == ("w1", "write", f"{HOME}/revisions/certs")
    assert json.loads((hosts.home("w1") / "revisions/certs").read_text()) == Workers().certificates("w1")
    assert all(certificate["synced"] for certificate in state("w1", "red").values())
    assert manager.checks == 1

  async def test_nothing_is_written_while_the_worker_holds_the_current_copy(self, manager, hosts, workers):
    await manager.distribute_certificates("w1")
    hosts.log.clear()
    await manager.distribute_certificates("w1")
    assert hosts.writes("w1") == []

  @pytest.mark.parametrize("drift", ["key", "extra", "config", "references", "stamp"])
  async def test_a_missing_or_unwanted_file_or_list_is_repaired_though_the_stamp_matches(self, manager, hosts, workers, drift):
    await manager.distribute_certificates("w1")
    home = hosts.home("w1")
    if drift == "key":
      next(managed(hosts, "w1").glob("worker-*.key")).unlink()
    elif drift == "extra":
      (managed(hosts, "w1") / "unwanted.key").write_text("obsolete")
    elif drift == "references":
      (home / "traefik/dynamic/certs.yml").write_text("tls:\n  certificates: []\n")
    elif drift == "stamp":
      (home / "revisions/certs").write_text("{}")
    else:
      (home / "traefik/dynamic/certs.yml").unlink()
    await manager.distribute_certificates("w1")
    assert len(list(managed(hosts, "w1").iterdir())) == 4 and len(listed(hosts, "w1")) == 2
    assert json.loads((home / "revisions/certs").read_text()) == Workers().certificates("w1")
    hosts.log.clear()
    await manager.distribute_certificates("w1")
    assert hosts.writes("w1") == []

  async def test_a_forced_copy_writes_everything_again(self, manager, hosts, workers):
    await manager.distribute_certificates("w1")
    hosts.log.clear()
    await manager.distribute_certificates("w1", force=True)
    assert len([entry for entry in hosts.writes("w1") if entry[1] == "write"]) == 6

  async def test_a_certificate_not_valid_is_taken_off_the_worker(self, manager, hosts, workers):
    await manager.distribute_certificates("w1")
    certificate = Certs().workers["w1"]
    write_certificate(certificate.fullchain_path.parent, certificate.names, timedelta(days=-1))
    Certs().reload(certificate)
    await manager.distribute_certificates("w1")
    assert [path.name.split("-")[0] for path in managed(hosts, "w1").iterdir()] == ["discovery"] * 2
    assert len(listed(hosts, "w1")) == 1 and set(Workers().certificates("w1")) == {"discovery"}
    assert not state("w1", "red")["worker"]["synced"]

  async def test_a_renamed_workers_old_wildcard_is_taken_off_before_its_new_one_is_issued(self, manager, hosts, workers):
    await manager.distribute_certificates("w1")
    Certs().set_workers({"w1": "green", "w2": "blue"})
    await manager.distribute_certificates("w1")
    assert not list(managed(hosts, "w1").glob("worker-*"))
    assert set(Workers().certificates("w1")) == {"discovery"}

  @pytest.mark.parametrize("found, setup", [("unreachable", "succeeded"), ("reachable", "failed"), ("reachable", None)])
  async def test_a_worker_not_reachable_and_set_up_is_left_alone(self, manager, hosts, workers, found, setup):
    if found == "unreachable":
      hosts.reachable.discard("w1")
    Workers().record_setup("w1", setup)
    await manager.distribute_certificates("w1")
    assert [entry for entry in hosts.log if entry[0] == "w1"] == []

  async def test_a_failed_copy_fails_the_task_and_writes_no_stamp(self, manager, hosts, workers, notices):
    hosts.fails = "certs.yml"
    with pytest.raises(TaskFailed):
      await manager.distribute_certificates("w1")
    [(level, content, link)] = notices()
    assert (level, content.startswith("The certificates could not be copied to worker w1: Error writing"), link) == (
        "error", True, f"{MANAGER}/workers/w1")
    assert not (hosts.home("w1") / "revisions/certs").exists()
    assert Workers().certificates("w1") is None and manager.checks == 1
