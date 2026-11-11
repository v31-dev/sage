from datetime import datetime, timedelta

from services.metrics.peaks import MinutePeaks, WorkerPeaks, host_row
from services.worker_reports import ContainerReading, HostReading, Snapshot

MINUTE = datetime(2026, 10, 23, 10, 5)


def host(idle=90.0, rx=None, tx=None, **overrides):
  values = dict(cpu_idle_percent=idle, cores=4, mem_total=8 * 1024**3, mem_used=1024**3, mem_cached=512 * 1024**2,
                mem_available=None, load1=0.5, load5=0.4, load15=0.3, disk_total=100 * 1024**3, disk_used=10 * 1024**3,
                net_rx_bytes=rx, net_tx_bytes=tx)
  return HostReading(**{**values, **overrides})


def container(name="web", status="running", cpu=100.0, mem=100 * 1024**2, rx=None, tx=None, started_at=1):
  return ContainerReading(name=name, status=status, image="img", labels={}, exit_code=0, oom_killed=False,
                          started_at=started_at, finished_at=None, health=None, cpu_percent=cpu, mem_bytes=mem,
                          net_rx_bytes=rx, net_tx_bytes=tx)


def snapshot(seconds, host_reading=None, *containers):
  return Snapshot(at=MINUTE + timedelta(seconds=seconds), host=host_reading or host(),
                  containers={c.name: c for c in containers}, traefik_reachable=True)


class TestMinutePeaks:
  def test_keeps_the_highest_reading_of_each_kind_in_a_minute(self):
    peaks = MinutePeaks()
    peaks.fold(MINUTE, {"cpu": 5, "mem": 100})
    peaks.fold(MINUTE, {"cpu": 9, "mem": 80})
    peaks.fold(MINUTE, {"cpu": 7})
    assert peaks.drain(MINUTE + timedelta(minutes=1)) == {MINUTE: {"cpu": 9, "mem": 100}}

  def test_a_reading_that_is_not_a_peak_keeps_its_latest_value(self):
    peaks = MinutePeaks()
    peaks.fold(MINUTE, {"cpu": 5}, {"cores": 2})
    peaks.fold(MINUTE, {"cpu": 1}, {"cores": 4})
    assert peaks.drain(MINUTE + timedelta(minutes=1)) == {MINUTE: {"cpu": 5, "cores": 4}}

  def test_drain_returns_only_the_minutes_before_the_given_one_and_removes_them(self):
    peaks = MinutePeaks()
    for minutes in range(3):
      peaks.fold(MINUTE + timedelta(minutes=minutes), {"cpu": minutes})
    assert peaks.drain(MINUTE + timedelta(minutes=2)) == {MINUTE: {"cpu": 0}, MINUTE + timedelta(minutes=1): {"cpu": 1}}
    assert len(peaks) == 1

  def test_minutes_that_were_never_drained_are_dropped_after_the_keep_window(self):
    peaks = MinutePeaks(keep=timedelta(minutes=10))
    peaks.fold(MINUTE, {"cpu": 1})
    peaks.fold(MINUTE + timedelta(minutes=11), {"cpu": 2})
    assert peaks.drain(MINUTE + timedelta(minutes=12)) == {MINUTE + timedelta(minutes=11): {"cpu": 2}}


class TestWorkerPeaks:
  def host_minute(self, peaks):
    return peaks.drain(MINUTE + timedelta(minutes=1))[0][MINUTE]

  def test_the_cpu_is_what_the_host_was_not_idle(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(idle=90)))
    peaks.fold(snapshot(10, host(idle=40)))
    peaks.fold(snapshot(20, host(idle=70)))
    assert self.host_minute(peaks)["cpu_pct"] == 60

  def test_memory_and_disk_are_stored_in_the_units_of_the_rows(self):
    minute = (lambda p: (p.fold(snapshot(0)), self.host_minute(p))[1])(WorkerPeaks())
    assert (minute["mem_used_mb"], minute["mem_cached_mb"], minute["disk_used_gb"]) == (1024, 512, 10.0)
    assert (minute["cpu_cores"], minute["mem_total_mb"], minute["disk_total_gb"]) == (4, 8192, 100.0)

  def test_the_network_rate_is_the_counter_change_over_the_time_between_reports(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=0, tx=0)))
    peaks.fold(snapshot(10, host(rx=102_400, tx=10_240)))
    minute = self.host_minute(peaks)
    assert (minute["net_rx_kbps"], minute["net_tx_kbps"]) == (10.0, 1.0)

  def test_an_idle_interface_is_a_rate_of_zero_not_a_missing_one(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=500, tx=500)))
    peaks.fold(snapshot(10, host(rx=500, tx=500)))
    minute = self.host_minute(peaks)
    assert (minute["net_rx_kbps"], minute["net_tx_kbps"]) == (0.0, 0.0)

  def test_the_first_report_has_no_network_rate(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=5_000_000, tx=5_000_000)))
    assert "net_rx_kbps" not in self.host_minute(peaks)

  def test_a_counter_that_went_down_gives_no_rate_for_that_report(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=1_000_000, tx=0)))
    peaks.fold(snapshot(10, host(rx=100, tx=0)))
    assert "net_rx_kbps" not in self.host_minute(peaks)

  def test_after_a_long_silence_a_counters_change_is_not_a_rate(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(rx=0, tx=0)))
    peaks.fold(snapshot(45, host(rx=10_000_000, tx=0)))
    assert "net_rx_kbps" not in self.host_minute(peaks)

  def test_a_repeated_or_older_snapshot_is_ignored(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(20, host(idle=50)))
    peaks.fold(snapshot(20, host(idle=0)))
    peaks.fold(snapshot(10, host(idle=0)))
    assert self.host_minute(peaks)["cpu_pct"] == 50

  def test_a_reading_the_report_lacks_is_left_out(self):
    minute = (lambda p: (p.fold(snapshot(0, host(idle=None, mem_used=None))), self.host_minute(p))[1])(WorkerPeaks())
    assert "cpu_pct" not in minute and "mem_used_mb" not in minute and "mem_cached_mb" in minute

  def test_a_running_container_shares_dockers_percentage_over_the_cores(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(cores=4), container(cpu=150.0, mem=300 * 1024**2)))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert containers["web"][MINUTE] == {"cpu_pct": 37.5, "mem_used_mb": 300}

  def test_a_container_that_is_not_running_is_left_out(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container(status="exited", cpu=0, mem=0), container("db")))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert set(containers) == {"db"}

  def test_a_containers_network_rate_comes_from_its_own_counters(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container(rx=1_000, tx=0)))
    peaks.fold(snapshot(10, host(), container(rx=1_000 + 20_480, tx=10_240)))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert (containers["web"][MINUTE]["net_rx_kbps"], containers["web"][MINUTE]["net_tx_kbps"]) == (2.0, 1.0)

  def test_a_new_run_of_a_container_has_no_rate_even_if_its_counter_is_higher(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container(rx=1_000, started_at=1)))
    peaks.fold(snapshot(10, host(), container(rx=900_000, started_at=2)))
    _, containers = peaks.drain(MINUTE + timedelta(minutes=1))
    assert "net_rx_kbps" not in containers["web"][MINUTE]

  def test_a_container_with_nothing_pending_is_forgotten(self):
    peaks = WorkerPeaks()
    peaks.fold(snapshot(0, host(), container()))
    peaks.drain(MINUTE + timedelta(minutes=1))
    assert peaks.containers == {}


class TestRows:
  def test_a_minute_lacking_a_core_reading_has_no_row(self):
    assert host_row(MINUTE, {"cpu_pct": 1, "mem_used_mb": 1, "mem_cached_mb": 1}) is None

  def test_the_network_rates_are_zero_and_the_load_is_none_when_not_reported(self):
    row = host_row(MINUTE, {"cpu_pct": 1, "mem_used_mb": 2, "mem_cached_mb": 3, "disk_used_gb": 4})
    assert (row["net_rx_kbps"], row["net_tx_kbps"], row["load_avg_1m"]) == (0.0, 0.0, None)
