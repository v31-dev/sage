import random
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

import config
from services.db import Worker
from services.manager.decisions import WorkerInputs, WorkerReboot, worker_entry, worker_with_row, worker_without_row
from services.manager.workers import WorkersMixin
from services.updates import Updates, UpdateSource
from services.worker_reports import WorkerReports
from tests.services.manager.test_decisions import report


class Changes:
  def __init__(self):
    self.sources = []
    self.keyed = []

  def changed(self, source, key=None):
    (self.sources if key is None else self.keyed).append(source if key is None else (source, key))


class Workers(WorkersMixin):
  """The part of a manager that keeps the worker list: the owners push, the clock is the test's."""

  def __init__(self, hold=0.0):
    self._worker_inputs = {}
    self.worker_list = []
    self._worker_list_lock = threading.Lock()
    self.paths = {}
    self.hold = hold
    self.tailscale = SimpleNamespace(path=lambda ip: self.paths.get(ip))

  def _edit_workers(self, edit):
    def slow(records):
      if self.hold:
        time.sleep(random.random() * self.hold)  # lets threads meet inside the lock
      edit(records)
    super()._edit_workers(slow)

  def add(self, hostname, online=None, ip="100.64.0.2"):
    """A worker with a row, whose online is what the sync observed."""
    self._save_worker(hostname, ip)
    if online is not None:
      self.update_worker(hostname, online=online)


@pytest.fixture
def changes(register):
  return register(Updates, Changes())


@pytest.fixture
def workers(db, clock, changes):
  WorkerReports()
  return Workers()


def entries(workers):
  return {worker["hostname"]: worker for worker in workers.worker_list}


class TestEntries:
  def test_each_worker_is_what_its_owners_pushed(self, workers):
    workers.paths["100.64.0.2"] = "direct"
    workers.add("w2", False, "100.64.0.3")
    workers.add("w1", True, "100.64.0.2")
    workers.update_worker("w1", report=report(running=("traefik", "web", "telegraf")))

    assert workers.worker_list == [
        {"hostname": "w1", "ip": "100.64.0.2", "online": True, "status": "online", "path": "direct", "container_count": 3,
         "infra": [{"name": "traefik", "status": "running"}, {"name": "vector", "status": "missing"},
                   {"name": "telegraf", "status": "running"}, {"name": "cloudflared", "status": "missing"}]},
        {"hostname": "w2", "ip": "100.64.0.3", "online": False, "status": "offline", "path": None, "container_count": None,
         "infra": None},
    ]

  def test_the_order_is_by_name(self, workers):
    for hostname in ("w3", "w1", "w2"):
      workers.add(hostname, True)
    assert [worker["hostname"] for worker in workers.worker_list] == ["w1", "w2", "w3"]

  def test_a_push_changes_only_the_fields_it_names(self, workers):
    workers.add("w1", True)
    workers.update_worker("w1", path="relay blr")
    workers.update_worker("w1", report=report())
    assert (entries(workers)["w1"]["online"], entries(workers)["w1"]["path"], entries(workers)["w1"]["container_count"]) == (True, "relay blr", 2)

  def test_a_name_that_is_not_a_field_is_refused_and_leaves_the_list_working(self, workers):
    workers.add("w1", True)
    with pytest.raises(TypeError):
      workers.update_worker("w1", onilne=True)
    workers.update_worker("w1", online=False)
    assert entries(workers)["w1"]["online"] is False

  def test_the_status_follows_the_tasks_the_queue_gave(self, workers):
    workers.add("w1", True)
    workers.add("w2", True, "100.64.0.3")
    workers._set_worker_tasks((("reboot_worker", "w1"), ("remove_worker", "w2")))
    assert [(worker["hostname"], worker["status"]) for worker in workers.worker_list] == [("w1", "rebooting"), ("w2", "removing")]
    workers._set_worker_tasks(())
    assert {worker["status"] for worker in workers.worker_list} == {"online"}


class TestPresence:
  def test_pushes_for_a_name_without_a_row_make_no_worker(self, workers):
    workers.update_worker("w9", online=True, path="direct", report=report())
    assert workers.worker_list == []
    assert workers.is_worker_online("w9") is False and workers.worker_counts() == {"total": 0, "online": 0}

  def test_a_report_that_came_before_the_row_is_there_when_the_row_is(self, workers, clock):
    workers.update_worker("w1", report=report(arrived_at=clock.current))
    workers.add("w1")
    assert entries(workers)["w1"]["container_count"] == 2

  def test_a_task_queued_before_the_row_shows_when_the_row_is_written(self, workers):
    workers._set_worker_tasks((("add_worker", "w1"),))
    assert workers.worker_list == []
    workers.add("w1")
    assert entries(workers)["w1"]["status"] == "adding"

  def test_a_worker_that_gets_a_row_is_checking_and_not_rebooting_whatever_was_pushed_before(self, workers):
    workers.update_worker("w1", online=True, reboot=WorkerReboot(WorkerReports().started, None))
    workers.add("w1")
    assert (entries(workers)["w1"]["online"], entries(workers)["w1"]["status"]) == (None, "checking")

  def test_writing_the_row_of_a_worker_again_keeps_it_online_and_follows_the_ip(self, workers):
    workers.add("w1", True)
    workers.paths["100.64.0.9"] = "direct"
    workers._save_worker("w1", "100.64.0.9")
    assert entries(workers)["w1"] == {"hostname": "w1", "ip": "100.64.0.9", "online": True, "status": "online", "path": "direct",
                                      "container_count": None, "infra": None}
    assert Worker.get(Worker.hostname == "w1").ip == "100.64.0.9"

  def test_forgetting_a_worker_deletes_its_row_and_its_entry(self, workers):
    workers.add("w1", True)
    workers._forget_worker("w1")
    assert workers.worker_list == [] and Worker.get_or_none(Worker.hostname == "w1") is None
    assert workers.is_worker_online("w1") is False

  def test_a_push_that_comes_after_the_worker_was_forgotten_makes_no_worker_and_is_not_inherited_by_the_next_of_the_name(self, workers):
    workers.add("w1", True)
    workers._forget_worker("w1")
    workers.update_worker("w1", online=True)
    assert workers.worker_list == []
    workers.add("w1")
    assert entries(workers)["w1"]["online"] is None

  def test_a_name_with_nothing_left_to_hold_has_no_record(self, workers):
    workers._set_worker_tasks((("add_worker", "w1"),))
    workers._set_worker_tasks(())
    workers.add("w2")
    workers._forget_worker("w2")
    assert workers._worker_inputs == {}

  def test_the_tasks_of_a_forgotten_worker_stay_until_the_queue_says_otherwise(self, workers):
    workers.add("w1", True)
    workers._set_worker_tasks((("remove_worker", "w1"),))
    workers._forget_worker("w1")
    assert set(workers._worker_inputs) == {"w1"} and workers.worker_list == []
    workers._set_worker_tasks(())
    assert workers._worker_inputs == {}


class TestOnline:
  def test_a_worker_nothing_is_known_of_is_not_online(self, workers):
    assert workers.is_worker_online("w1") is False

  def test_a_new_worker_is_not_online_until_it_is_set_so(self, workers):
    workers.add("w1")
    assert workers.is_worker_online("w1") is False
    workers.update_worker("w1", online=True)
    assert workers.is_worker_online("w1") is True
    workers.update_worker("w1", online=False)
    assert workers.is_worker_online("w1") is False

  def test_only_the_online_workers_are_the_online_ones(self, workers):
    workers.add("w1", True)
    workers.add("w2", False)
    workers.add("w3")
    assert [worker.hostname for worker in workers.online_workers()] == ["w1"]

  def test_a_worker_counts_as_online_when_its_status_is_online(self, workers):
    workers.add("w1", True)
    workers.add("w2", True)
    workers.add("w3", False)
    workers._set_worker_tasks((("reboot_worker", "w2"),))
    assert workers.worker_counts() == {"total": 3, "online": 1}

  def test_the_entry_of_a_worker_is_the_one_in_the_list_and_none_for_a_name_that_is_not_listed(self, workers):
    workers.add("w1", True)
    assert workers.worker_entry("w1") is workers.worker_list[0]
    assert workers.worker_entry("w9") is None

  def test_the_status_of_a_name_that_is_not_listed_is_offline(self, workers):
    assert workers.worker_status("w9") == "offline"

  def test_the_status_of_a_worker_is_the_one_in_the_list(self, workers):
    workers.add("w1", True)
    workers._set_worker_tasks((("remove_worker", "w1"),))
    assert workers.worker_status("w1") == "removing"


class TestLoad:
  def test_the_table_is_read_into_the_list(self, workers):
    Worker.create(hostname="w2", ip="100.64.0.3")
    Worker.create(hostname="w1", ip="100.64.0.2")
    workers.paths["100.64.0.2"] = "direct"
    workers.load_workers()
    assert [(worker["hostname"], worker["path"], worker["online"]) for worker in workers.worker_list] == [
        ("w1", "direct", None), ("w2", None, None)]

  def test_a_worker_that_is_in_the_table_before_and_after_keeps_its_online(self, workers):
    workers.add("w1", True)
    workers.load_workers()
    assert workers.is_worker_online("w1") is True

  def test_a_worker_the_table_no_longer_has_goes_and_a_new_one_is_not_known_yet(self, workers):
    workers.add("w1", True)
    Worker.delete().execute()
    Worker.create(hostname="w2", ip="100.64.0.3")
    workers.update_worker("w2", online=True)
    workers.load_workers()
    assert [(worker["hostname"], worker["online"]) for worker in workers.worker_list] == [("w2", None)]

  def test_it_is_the_only_read_of_the_table(self, workers):
    workers.add("w1", True)
    Worker.create(hostname="w9", ip="100.64.0.9")
    workers.update_worker("w1", path="direct")
    workers._set_worker_tasks(())
    assert [worker["hostname"] for worker in workers.worker_list] == ["w1"]


class TestFiringForOneWorker:
  def test_a_push_that_changes_one_workers_entry_fires_that_worker_only(self, workers, changes):
    workers.add("w1", True)
    workers.add("w2", True, "100.64.0.3")
    changes.keyed.clear()
    workers.update_worker("w2", path="relay blr")
    assert changes.keyed == [(UpdateSource.WORKER_ENTRY, "w2")]

  def test_a_new_worker_and_a_forgotten_one_fire_theirs(self, workers, changes):
    workers.add("w1", True)
    assert (UpdateSource.WORKER_ENTRY, "w1") in changes.keyed
    changes.keyed.clear()
    workers._forget_worker("w1")
    assert changes.keyed == [(UpdateSource.WORKER_ENTRY, "w1")]

  def test_what_the_clock_changed_fires_the_workers_it_changed(self, workers, clock, changes):
    workers.add("w1", True)
    workers.add("w2", True, "100.64.0.3")
    workers.update_worker("w1", report=report(arrived_at=clock.current))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS - 5))
    workers.update_worker("w2", report=report(arrived_at=clock.current))
    changes.keyed.clear()
    clock.advance(timedelta(seconds=10))
    workers.update_workers({})
    assert changes.keyed == [(UpdateSource.WORKER_ENTRY, "w1")]

  def test_a_push_that_changes_nothing_fires_no_worker(self, workers, changes):
    workers.add("w1", True)
    changes.keyed.clear()
    workers.update_worker("w1", online=True)
    workers.update_worker("w9", online=True)
    assert changes.keyed == []


class TestFiring:
  def test_a_list_that_differs_fires_the_list_source(self, workers, changes):
    workers.add("w1", True)
    assert changes.sources == [UpdateSource.WORKER_LIST] * 2

  def test_a_push_that_changes_nothing_fires_nothing(self, workers, changes):
    workers.add("w1", True)
    changes.sources.clear()
    workers.update_worker("w1", online=True)
    workers._set_worker_tasks(())
    workers.update_worker("w9", online=False)
    assert changes.sources == []

  @pytest.mark.parametrize("push", [
      lambda workers: workers.update_worker("w1", online=False),
      lambda workers: workers.update_worker("w1", path="relay blr"),
      lambda workers: workers.update_worker("w1", report=report()),
      lambda workers: workers._set_worker_tasks((("reboot_worker", "w1"),)),
  ], ids=["online", "path", "container count", "task"])
  def test_a_push_that_changes_what_an_entry_shows_fires_it_once(self, workers, changes, push):
    workers.add("w1", True)
    changes.sources.clear()
    push(workers)
    assert changes.sources == [UpdateSource.WORKER_LIST]

  def test_a_report_that_shows_the_same_as_the_last_one_fires_nothing(self, workers, changes, clock):
    workers.add("w1", True)
    workers.update_worker("w1", report=report(arrived_at=clock.current))
    changes.sources.clear()
    clock.advance(timedelta(seconds=10))
    workers.update_worker("w1", report=report(arrived_at=clock.current))
    assert changes.sources == []

  def test_a_worker_that_is_forgotten_fires_the_list(self, workers, changes):
    workers.add("w1", True)
    changes.sources.clear()
    workers._forget_worker("w1")
    assert changes.sources == [UpdateSource.WORKER_LIST]


class TestTheClock:
  """What only the clock changes shows the next time the list is worked out, which every push does."""

  def test_a_report_that_goes_stale_leaves_the_worker_not_ready_and_without_a_count(self, workers, clock, changes):
    workers.add("w1", True)
    workers.update_worker("w1", report=report(arrived_at=clock.current))
    assert (entries(workers)["w1"]["status"], entries(workers)["w1"]["container_count"]) == ("online", 2)
    changes.sources.clear()
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    workers.update_workers({})
    assert (entries(workers)["w1"]["status"], entries(workers)["w1"]["container_count"]) == ("not ready", None)
    assert changes.sources == [UpdateSource.WORKER_LIST]

  def test_the_end_of_the_grace_makes_a_silent_worker_not_ready(self, workers, clock):
    workers.add("w1", True)
    assert entries(workers)["w1"]["status"] == "online"
    clock.advance(timedelta(seconds=config.REPORT_STARTUP_GRACE_SECONDS))
    workers.update_workers({})
    assert entries(workers)["w1"]["status"] == "not ready"

  def test_the_end_of_a_reboots_limit_ends_its_status(self, workers, clock):
    workers.add("w1", False)
    workers.update_worker("w1", reboot=WorkerReboot(clock.current, None))
    assert entries(workers)["w1"]["status"] == "rebooting"
    clock.advance(timedelta(seconds=config.WORKER_REBOOT_LIMIT_SECONDS + 1))
    workers.update_workers({})
    assert entries(workers)["w1"]["status"] == "offline"

  def test_a_push_for_one_worker_shows_what_the_clock_did_to_another(self, workers, clock):
    workers.add("w1", True)
    workers.add("w2", True, "100.64.0.3")
    workers.update_worker("w1", report=report(arrived_at=clock.current))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    workers.update_worker("w2", report=report(arrived_at=clock.current))
    assert [(worker["hostname"], worker["status"]) for worker in workers.worker_list] == [("w1", "not ready"), ("w2", "online")]

  def test_working_the_list_out_again_with_the_clock_unchanged_fires_nothing(self, workers, changes):
    workers.add("w1", True)
    changes.sources.clear()
    workers.update_workers({})
    assert changes.sources == []


class TestFromManyThreads:
  TASKS = (None, "add_worker", "remove_worker", "reboot_worker")

  @staticmethod
  def expected_status(online, task):
    return {"add_worker": "adding", "remove_worker": "removing", "reboot_worker": "rebooting"}.get(task) or ("online" if online else "offline")

  @pytest.mark.parametrize("seed", range(6))
  def test_after_pushes_from_many_threads_the_list_holds_what_each_owner_last_pushed(self, workers, monkeypatch, seed):
    rng = random.Random(seed)
    workers = Workers(hold=0.002)
    entry = worker_entry

    def slow_entry(*args):
      time.sleep(rng_sleep.random() * 0.002)  # working out an entry takes a while, so threads that do not wait for each other interleave
      return entry(*args)
    rng_sleep = random.Random(seed)
    monkeypatch.setattr("services.manager.workers.worker_entry", slow_entry)
    names = [f"w{index}" for index in range(5)]
    for index, name in enumerate(names):
      workers.add(name, True, f"100.64.0.{index + 2}")
    finals = {name: (rng.random() < 0.5, rng.choice(self.TASKS), rng.choice([None, "direct", "relay blr"])) for name in names}
    flapper_present = rng.random() < 0.5

    together = threading.Barrier(len(names))

    def push(name, thread_seed):
      thread_rng = random.Random(thread_seed)
      for _ in range(20):
        workers.update_worker(name, online=thread_rng.random() < 0.5, path=thread_rng.choice([None, "direct"]))
      together.wait(10)  # the last pushes of all the owners meet
      online, task, path = finals[name]
      workers.update_worker(name, online=online, path=path, tasks=frozenset({task} - {None}))

    def flap():
      # the writes of a row without the table, which these threads would each need a connection for
      def give(records):
        records["wx"] = worker_with_row(records.get("wx", WorkerInputs()), "100.64.0.99", None)

      def take(records):
        records["wx"] = worker_without_row(records.get("wx", WorkerInputs()))
      thread_rng = random.Random(seed)
      for _ in range(20):
        workers._edit_workers(give if thread_rng.random() < 0.5 else take)
      workers._edit_workers(give if flapper_present else take)
      workers.update_worker("wx", online=True)

    threads = [threading.Thread(target=push, args=(name, rng.random())) for name in names] + [threading.Thread(target=flap)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()

    expected = {name: {"hostname": name, "ip": f"100.64.0.{index + 2}", "online": finals[name][0],
                       "status": self.expected_status(finals[name][0], finals[name][1]), "path": finals[name][2],
                       "container_count": None, "infra": None}
                for index, name in enumerate(names)}
    if flapper_present:
      expected["wx"] = {"hostname": "wx", "ip": "100.64.0.99", "online": True, "status": "online", "path": None, "container_count": None,
                        "infra": None}
    assert workers.worker_list == [expected[name] for name in sorted(expected)]
