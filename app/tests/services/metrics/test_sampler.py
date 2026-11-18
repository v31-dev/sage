import os
from pathlib import Path

import pytest

import config
from services.metrics.sampler import read_counters, read_sample

MIB = 1024**2


@pytest.fixture
def cgroup(tmp_path, monkeypatch):
  """The manager's cgroup as files: `cgroup(**{"cpu.max": "max"})` changes one."""
  monkeypatch.setattr(config, "CGROUP_ROOT", tmp_path)

  def write(**files):
    defaults = {"cpu.stat": "usage_usec 5000000\nuser_usec 4000000\n", "cpu.max": "200000 100000\n",
                "memory.current": f"{300 * MIB}\n", "memory.stat": f"anon {200 * MIB}\nfile {100 * MIB}\n",
                "memory.max": f"{1024 * MIB}\n"}
    for name, content in {**defaults, **files}.items():
      (tmp_path / name).write_text(content)
  write()
  return write


def test_the_counters_are_the_cgroups_cpu_time_and_the_bytes_of_the_interfaces(cgroup):
  cpu, rx, tx, at = read_counters()
  assert cpu == 5_000_000
  assert rx >= 0 and tx >= 0 and at > 0


def test_the_cpu_is_the_time_used_over_the_containers_allowance_and_the_network_the_bytes_a_second(cgroup):
  peaks, _ = read_sample((0, 0, 0, 0.0), (1_000_000, 20 * 1024, 10 * 1024, 10.0))
  assert (peaks["cpu_pct"], peaks["net_rx_kbps"], peaks["net_tx_kbps"]) == (5.0, 2.0, 1.0)


def test_a_counter_that_went_down_is_no_usage(cgroup):
  peaks, _ = read_sample((9_000_000, 50_000, 50_000, 0.0), (1_000_000, 100, 100, 10.0))
  assert (peaks["cpu_pct"], peaks["net_rx_kbps"], peaks["net_tx_kbps"]) == (0.0, 0.0, 0.0)


def test_memory_is_read_in_megabytes_against_the_cgroups_limit(cgroup):
  peaks, latest = read_sample((0, 0, 0, 0.0), (0, 0, 0, 10.0))
  assert (peaks["mem_used_mb"], peaks["mem_cached_mb"]) == (300, 100)
  assert (latest["cpu_cores"], latest["mem_total_mb"]) == (2, 1024)
  assert latest["disk_total_gb"] > 0 and 0 <= peaks["disk_used_gb"] <= latest["disk_total_gb"]


def test_without_limits_the_allowance_is_the_hosts_cores_and_the_total_its_memory(cgroup):
  cgroup(**{"cpu.max": "max 100000\n", "memory.max": "max\n"})
  peaks, latest = read_sample((0, 0, 0, 0.0), (os.cpu_count() * 1_000_000, 0, 0, 1.0))
  total = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
               if line.startswith("MemTotal:"))
  assert (peaks["cpu_pct"], latest["cpu_cores"], latest["mem_total_mb"]) == (100.0, os.cpu_count(), total // MIB)
