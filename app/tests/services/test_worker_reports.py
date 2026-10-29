import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
from services.worker_reports import WorkerReports, container_started_at, infra_of, is_current, parse_batch

BATCH = json.loads((Path(__file__).parent.parent / "data" / "telegraf_batch.json").read_text())


def metric(name, timestamp=1_000_000, tags=None, **fields):
  return {"name": name, "tags": tags or {}, "fields": fields, "timestamp": timestamp}


def status(name, timestamp=1_000_000, **tags):
  return metric("docker_container_status", timestamp, {"container_name": name, "container_status": "running", **tags},
                exitcode=0, oomkilled=False, started_at=5)


class TestParseBatch:
  def test_two_gathers_make_two_snapshots_oldest_first(self):
    first, second = parse_batch(BATCH)
    assert second.at - first.at == timedelta(seconds=config.REPORT_INTERVAL_SECONDS)

  def test_one_snapshot_holds_metrics_stamped_seconds_apart(self):
    # The recorded gather is stamped over three seconds (the Docker input takes a while).
    stamps = {m["timestamp"] // 1000 for m in BATCH["metrics"]}
    first = parse_batch(BATCH)[0]
    assert len(stamps) == 6 and len(parse_batch(BATCH)) == 2
    assert {"web", "traefik", "old-job"} == set(first.containers)

  def test_host_reading(self):
    host = parse_batch(BATCH)[0].host
    assert host.cores == 16
    assert (host.mem_total, host.mem_used, host.mem_cached) == (30769758208, 17019699200, 18287808512)
    assert host.cpu_idle_percent == pytest.approx(89.2106, abs=1e-3)
    assert (host.load1, host.load5, host.load15) == (0.45, 0.42, 0.49)
    assert (host.disk_total, host.disk_used) == (15384879104, 6611550208)
    assert (host.net_rx_bytes, host.net_tx_bytes) == (54080713481, 869735831)

  def test_container_readings_join_the_measurements_by_name(self):
    web = parse_batch(BATCH)[0].containers["web"]
    assert web.status == "running"
    assert web.deployed_at == "2026-10-24T23:08:35.718155"
    assert web.health == "healthy"
    assert web.exit_code == 0 and web.oom_killed is False
    assert web.mem_bytes is not None and web.cpu_percent is not None

  def test_an_oom_kill_is_reported(self):
    killed = status("db")
    killed["fields"]["oomkilled"] = True
    snapshot, = parse_batch({"metrics": [killed, status("web")]})
    assert snapshot.containers["db"].oom_killed is True
    assert snapshot.containers["web"].oom_killed is False

  def test_a_container_that_stopped_carries_its_exit_and_finish(self):
    old = parse_batch(BATCH)[0].containers["old-job"]
    assert old.status == "exited"
    assert old.finished_at is not None and old.started_at < old.finished_at
    assert old.deployed_at is None

  def test_the_traefik_check(self):
    assert parse_batch(BATCH)[0].traefik_reachable is True

  @pytest.mark.parametrize("result_code, expected", [(0, True), (2, False)])
  def test_traefik_is_reachable_only_when_the_check_succeeded(self, result_code, expected):
    snapshot, = parse_batch({"metrics": [metric("net_response", result_code=result_code)]})
    assert snapshot.traefik_reachable is expected

  def test_no_check_in_the_report_is_unknown_not_down(self):
    snapshot, = parse_batch({"metrics": [metric("cpu", usage_idle=90)]})
    assert snapshot.traefik_reachable is None

  def test_network_counters_are_summed_over_interfaces_without_the_protocol_totals(self):
    batch = {"metrics": [
        metric("net", tags={"interface": "eth0"}, bytes_recv=100, bytes_sent=10),
        metric("net", tags={"interface": "ens4"}, bytes_recv=50, bytes_sent=5),
        metric("net", tags={"interface": "all"}, bytes_recv=9999, bytes_sent=9999),
    ]}
    host = parse_batch(batch)[0].host
    assert (host.net_rx_bytes, host.net_tx_bytes) == (150, 15)

  def test_a_measurement_that_is_missing_is_none(self):
    host = parse_batch({"metrics": [metric("cpu", usage_idle=90)]})[0].host
    assert host.cores is None and host.mem_total is None and host.net_rx_bytes is None

  def test_a_container_with_no_status_record_is_left_out(self):
    batch = {"metrics": [
        metric("docker_container_mem", tags={"container_name": "orphan"}, usage=5),
        status("web"),
    ]}
    assert set(parse_batch(batch)[0].containers) == {"web"}

  def test_a_gap_between_gathers_makes_separate_snapshots(self):
    interval_ms = config.REPORT_INTERVAL_SECONDS * 1000
    batch = {"metrics": [metric("cpu", 0, usage_idle=1), metric("cpu", 3 * interval_ms, usage_idle=2)]}
    assert [s.host.cpu_idle_percent for s in parse_batch(batch)] == [1, 2]

  def test_an_empty_batch_has_no_snapshots(self):
    assert parse_batch({"metrics": []}) == []

  @pytest.mark.parametrize("batch", [
      [], "text", {}, {"metrics": "x"},
      {"metrics": [None]},
      {"metrics": [{"name": "cpu", "fields": {}, "timestamp": 1}]},
      {"metrics": [{"name": "cpu", "tags": {}, "timestamp": 1}]},
      {"metrics": [{"name": "cpu", "tags": {}, "fields": {}}]},
      {"metrics": [{"name": "cpu", "tags": {}, "fields": {}, "timestamp": True}]},
      {"metrics": [{"name": 5, "tags": {}, "fields": {}, "timestamp": 1}]},
  ])
  def test_a_batch_that_is_not_telegrafs_json_is_refused(self, batch):
    with pytest.raises(ValueError):
      parse_batch(batch)


def snapshots_at(seconds):
  return parse_batch({"metrics": [metric("cpu", seconds * 1000, usage_idle=seconds)]})


class TestReports:
  def test_keeps_the_newest_snapshot_with_its_arrival(self, clock):
    reports = WorkerReports()
    reports.accept("w1", snapshots_at(10))
    clock.advance(timedelta(seconds=10))
    reports.accept("w1", snapshots_at(20))
    received = reports.latest("w1")
    assert received.snapshot.host.cpu_idle_percent == 20
    assert received.arrived_at == datetime(2026, 1, 1, 12, 0, 10)

  def test_takes_the_last_snapshot_of_a_batch_that_holds_several(self):
    reports = WorkerReports()
    reports.accept("w1", snapshots_at(10) + snapshots_at(20) + snapshots_at(30))
    assert reports.latest("w1").snapshot.host.cpu_idle_percent == 30

  def test_a_replay_older_than_what_is_held_is_ignored(self, clock):
    reports = WorkerReports()
    reports.accept("w1", snapshots_at(30))
    clock.advance(timedelta(seconds=5))
    assert reports.accept("w1", snapshots_at(10)) is None
    received = reports.latest("w1")
    assert received.snapshot.host.cpu_idle_percent == 30
    assert received.arrived_at == datetime(2026, 1, 1, 12, 0, 0)

  def test_each_worker_has_its_own(self):
    reports = WorkerReports()
    reports.accept("w1", snapshots_at(10))
    reports.accept("w2", snapshots_at(20))
    assert reports.latest("w1").snapshot.host.cpu_idle_percent == 10
    assert reports.latest("w2").snapshot.host.cpu_idle_percent == 20
    assert reports.latest("w3") is None

  def test_the_first_report_and_the_first_after_a_silence_are_flagged(self, clock):
    reports = WorkerReports()
    assert reports.accept("w1", snapshots_at(10)).resumed is True
    clock.advance(timedelta(seconds=config.REPORT_INTERVAL_SECONDS))
    assert reports.accept("w1", snapshots_at(20)).resumed is False
    clock.advance(timedelta(seconds=config.REPORT_INTERVAL_SECONDS * 3 + 1))
    assert reports.accept("w1", snapshots_at(30)).resumed is True

  def test_an_empty_batch_changes_nothing(self):
    reports = WorkerReports()
    assert reports.accept("w1", []) is None
    assert reports.latest("w1") is None

  def test_forget_drops_a_worker(self):
    reports = WorkerReports()
    reports.accept("w1", snapshots_at(10))
    reports.forget("w1")
    reports.forget("never-seen")
    assert reports.latest("w1") is None

  def test_an_unknown_name_is_first_only_once(self):
    reports = WorkerReports()
    assert reports.first_unknown("stranger") is True
    assert reports.first_unknown("stranger") is False

  def test_names_that_keep_changing_do_not_grow_what_is_remembered(self, monkeypatch):
    monkeypatch.setattr(config, "REPORT_UNKNOWN_LIMIT", 3)
    reports = WorkerReports()
    assert all(reports.first_unknown(f"stranger-{n}") for n in range(10))
    assert len(reports._unknown) <= 3

  def test_a_name_still_remembered_is_not_first_again(self, monkeypatch):
    monkeypatch.setattr(config, "REPORT_UNKNOWN_LIMIT", 3)
    reports = WorkerReports()
    for name in ("a", "b", "c"):
      reports.first_unknown(name)
    assert reports.first_unknown("c") is False


def with_containers(seconds, *readings):
  host = parse_batch({"metrics": [metric("cpu", seconds * 1000, usage_idle=90)]})[0].host
  from services.worker_reports import Snapshot
  base = snapshots_at(seconds)[0]
  return [Snapshot(at=base.at, host=host, containers={c.name: c for c in readings}, traefik_reachable=True)]


def reading(name, status="running", started_at=1, deployed_at="stamp"):
  from services.worker_reports import ContainerReading
  return ContainerReading(name=name, status=status, image="img", deployed_at=deployed_at, exit_code=0, oom_killed=False,
                          started_at=started_at, finished_at=None, health=None, cpu_percent=None, mem_bytes=None,
                          net_rx_bytes=None, net_tx_bytes=None)


class TestWhatChanged:
  def test_the_first_report_changes_every_container(self):
    arrival = WorkerReports().accept("w1", with_containers(10, reading("web"), reading("db")))
    assert arrival.changed == {"web", "db"}

  def test_a_report_that_is_the_same_changes_nothing(self):
    reports = WorkerReports()
    reports.accept("w1", with_containers(10, reading("web")))
    assert reports.accept("w1", with_containers(20, reading("web"))).changed == frozenset()

  @pytest.mark.parametrize("after", [
      reading("web", status="restarting"),
      reading("web", started_at=2),
      reading("web", deployed_at="newer"),
  ])
  def test_a_new_state_start_or_version_changes_the_container(self, after):
    reports = WorkerReports()
    reports.accept("w1", with_containers(10, reading("web"), reading("db")))
    assert reports.accept("w1", with_containers(20, after, reading("db"))).changed == {"web"}

  def test_a_container_that_appeared_or_vanished_changes(self):
    reports = WorkerReports()
    reports.accept("w1", with_containers(10, reading("web"), reading("db")))
    assert reports.accept("w1", with_containers(20, reading("web"), reading("cache"))).changed == {"db", "cache"}


class TestArrival:
  def test_it_carries_the_report_now_held(self):
    reports = WorkerReports()
    arrival = reports.accept("w1", snapshots_at(10))
    assert arrival.received is reports.latest("w1")
    assert reports.accept("w1", snapshots_at(20)).received is reports.latest("w1")


class TestCurrentAndTrusted:
  def test_a_report_is_current_for_the_stale_window(self, clock):
    reports = WorkerReports()
    reports.accept("w1", snapshots_at(10))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS))
    assert reports.current("w1") is not None
    clock.advance(timedelta(seconds=1))
    assert reports.current("w1") is None

  def test_a_worker_that_never_reported_has_no_current_report(self):
    assert WorkerReports().current("w1") is None

  def test_a_snapshot_counts_only_if_it_arrived_after_the_last_write_and_the_settle_time(self, clock):
    reports = WorkerReports()
    wrote = clock.current
    clock.advance(timedelta(seconds=config.REPORT_SETTLE_SECONDS - 1))
    reports.accept("w1", snapshots_at(10))
    assert reports.trusted("w1", wrote) is None

  def test_a_snapshot_that_arrived_after_the_settle_time_counts(self, clock):
    reports = WorkerReports()
    wrote = clock.current
    clock.advance(timedelta(seconds=config.REPORT_SETTLE_SECONDS))
    reports.accept("w1", snapshots_at(10))
    assert reports.trusted("w1", wrote) is not None

  def test_a_snapshot_that_is_no_longer_current_does_not_count(self, clock):
    reports = WorkerReports()
    wrote = clock.current - timedelta(minutes=5)
    reports.accept("w1", snapshots_at(10))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    assert reports.trusted("w1", wrote) is None

  def test_the_seconds_since_the_last_report_or_since_the_start(self, clock):
    reports = WorkerReports()
    clock.advance(timedelta(seconds=40))
    assert reports.silent_for("w1") == 40
    reports.accept("w1", snapshots_at(10))
    clock.advance(timedelta(seconds=7))
    assert reports.silent_for("w1") == 7


class TestIsCurrent:
  def test_a_report_is_current_up_to_the_stale_window_and_not_after(self, clock):
    arrived = clock.current
    assert is_current(arrived, arrived + timedelta(seconds=config.REPORT_STALE_SECONDS))
    assert not is_current(arrived, arrived + timedelta(seconds=config.REPORT_STALE_SECONDS, microseconds=1))


class TestContainerStartedAt:
  def test_it_is_the_start_the_report_gives_the_container(self):
    received = WorkerReports().accept("w1", with_containers(10, reading("telegraf", started_at=777))).received
    assert container_started_at(received, "telegraf") == 777

  def test_it_is_none_without_a_report_or_for_a_container_it_does_not_list(self):
    received = WorkerReports().accept("w1", with_containers(10, reading("web"))).received
    assert container_started_at(None, "telegraf") is None
    assert container_started_at(received, "telegraf") is None


class TestInfra:
  def test_each_container_of_the_stack_has_its_state_and_a_missing_one_says_so(self):
    received = WorkerReports().accept("w1", with_containers(10, reading("traefik"), reading("vector", status="restarting"),
                                                            reading("telegraf"), reading("web"))).received
    assert infra_of(received) == [
        {"name": "traefik", "status": "running"},
        {"name": "vector", "status": "restarting"},
        {"name": "telegraf", "status": "running"},
        {"name": "cloudflared", "status": "missing"},
    ]


class TestStartedAt:
  def test_when_a_container_last_started_from_the_current_report(self):
    reports = WorkerReports()
    reports.accept("w1", with_containers(10, reading("telegraf", started_at=42)))
    assert reports.started_at("w1", "telegraf") == 42
    assert reports.started_at("w1", "traefik") is None

  def test_a_stale_report_gives_nothing_unless_asked_for_however_old(self, clock):
    reports = WorkerReports()
    reports.accept("w1", with_containers(10, reading("telegraf", started_at=42)))
    clock.advance(timedelta(seconds=config.REPORT_STALE_SECONDS + 1))
    assert reports.started_at("w1", "telegraf") is None
    assert reports.started_at("w1", "telegraf", current=False) == 42

  def test_a_worker_that_never_reported_has_none(self):
    assert WorkerReports().started_at("w1", "telegraf", current=False) is None
