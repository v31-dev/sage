"""The manager's own readings, from its container's cgroup, the network interfaces and the root filesystem."""
import os
import shutil
import time
from pathlib import Path

import config
from services.metrics.peaks import GIB, MIB


def read_counters() -> tuple[int, int, int, float]:
  """The counters a rate is taken between: CPU time used (microseconds), bytes received and sent on every interface
  but loopback, and when they were read (monotonic seconds)."""
  now = time.monotonic()

  cpu_usage_usec = next(
      int(line.split()[1])
      for line in (config.CGROUP_ROOT / "cpu.stat").read_text().splitlines()
      if line.startswith("usage_usec ")
  )
  interfaces = [
      line.split()
      for line in Path("/proc/net/dev").read_text().splitlines()[2:]
      if ":" in line
  ]
  rx_bytes = sum(int(f[1]) for f in interfaces if not f[0].startswith("lo:"))
  tx_bytes = sum(int(f[9]) for f in interfaces if not f[0].startswith("lo:"))
  return cpu_usage_usec, rx_bytes, tx_bytes, now


def read_sample(prev: tuple[int, int, int, float], current: tuple[int, int, int, float]) -> tuple[dict, dict]:
  """The peaks of a sample, the rates between the counters `prev` and `current` with the levels read now, and its
  latest values, the container's size. CPU is normalized against the container's CPU allowance (cgroup quota if
  limited, else host cores); memory total is the cgroup limit if set, else host memory; disk is the root filesystem so
  temp usage counts."""
  cpu_usage_usec, rx_bytes, tx_bytes, now = current
  elapsed = now - prev[3]

  cpu_max = (config.CGROUP_ROOT / "cpu.max").read_text().split()
  cpu_allowance = (os.cpu_count() or 1) if cpu_max[0] == "max" else int(cpu_max[0]) / int(cpu_max[1])
  cpu_pct = round(max(cpu_usage_usec - prev[0], 0) / (elapsed * 1_000_000) / cpu_allowance * 100, 2)
  net_rx_kbps = round(max(rx_bytes - prev[1], 0) / 1024 / elapsed, 2)
  net_tx_kbps = round(max(tx_bytes - prev[2], 0) / 1024 / elapsed, 2)

  mem_used = int((config.CGROUP_ROOT / "memory.current").read_text())
  mem_stat = dict(
      line.split(maxsplit=1) for line in (config.CGROUP_ROOT / "memory.stat").read_text().splitlines()
  )
  mem_cached = int(mem_stat.get("file", 0))
  mem_max = (config.CGROUP_ROOT / "memory.max").read_text().strip()
  if mem_max == "max":
    mem_total = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal:")
    )
  else:
    mem_total = int(mem_max)

  disk = shutil.disk_usage("/")

  return (
      {
          "cpu_pct": cpu_pct,
          "mem_used_mb": mem_used // MIB,
          "mem_cached_mb": mem_cached // MIB,
          "disk_used_gb": round(disk.used / GIB, 2),
          "net_rx_kbps": net_rx_kbps,
          "net_tx_kbps": net_tx_kbps,
      },
      {
          "cpu_cores": round(cpu_allowance),
          "mem_total_mb": mem_total // MIB,
          "disk_total_gb": round(disk.total / GIB, 2),
      },
  )
