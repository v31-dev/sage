"""The metrics files' migrations (`migrator.migrate`): a host's file's baseline and steps."""

# 0.1.3's host file (`metrics/<hostname>.db`), as it created it.
BASELINE = """
CREATE TABLE "containermetrics" ("name" VARCHAR(255) NOT NULL, "ts" DATETIME NOT NULL, "cpu_pct" REAL NOT NULL, "mem_used_mb" REAL NOT NULL, "net_rx_kbps" REAL NOT NULL, "net_tx_kbps" REAL NOT NULL, PRIMARY KEY ("name", "ts"));
CREATE TABLE "workermeta" ("id" INTEGER NOT NULL PRIMARY KEY, "cpu_cores" INTEGER NOT NULL, "mem_total_mb" INTEGER NOT NULL, "disk_total_gb" REAL NOT NULL);
CREATE TABLE "workermetrics" ("ts" DATETIME NOT NULL PRIMARY KEY, "cpu_pct" REAL NOT NULL, "mem_used_mb" INTEGER NOT NULL, "mem_cached_mb" INTEGER NOT NULL, "disk_used_gb" REAL NOT NULL, "load_avg_1m" REAL, "load_avg_5m" REAL, "load_avg_15m" REAL, "net_rx_kbps" REAL NOT NULL, "net_tx_kbps" REAL NOT NULL);
"""

STEPS = []
