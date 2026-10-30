"""Locations, endpoints and timings shared by the backend. Modules read a value as `config.NAME`
where it is used, never `from config import NAME`, so a test can override one attribute."""
from pathlib import Path

DATA_DIR = Path("/app/data")
DB_PATH = DATA_DIR / "data.db"
STATIC_DIR = Path("/app/static")
WORKER_HOME_DIR = "/opt/sage"
CGROUP_ROOT = Path("/sys/fs/cgroup")

CLOUDFLARE_API_URL = "https://api.cloudflare.com/client/v4"
ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
LATEST_RELEASE_URL = "https://api.github.com/repos/v31-dev/sage/releases/latest"

# How long history is kept: logs, metrics, events, notifications, task history, restore
# safety copies, and S3 platform and volume backups (the daily cleanup).
RETENTION_DAYS = 14

# The Socket.IO connection: the first change starts this wait, and the topics that depend on the changes made within it
# are recomputed together once it ends.
UPDATES_COALESCE_SECONDS = 3
# The server pings every connection this often, and drops one that does not answer within the timeout.
UPDATES_PING_INTERVAL_SECONDS = 10
UPDATES_PING_TIMEOUT_SECONDS = 10

# Log size limits. A container's logs, or the task logs, are trimmed, oldest lines first, when their data passes
# LOG_SHARD_MAX_BYTES, and all the logs when together they pass the smaller of LOG_TOTAL_MAX_BYTES and
# LOG_TOTAL_MAX_DISK_SHARE of the data volume. A trim leaves LOG_TRIM_TO_SHARE of the limit and keeps each file's newest
# LOG_KEEP_NEWEST_ROWS lines. Sizes are checked after a write at most every LOG_SIZE_CHECK_SECONDS, and lines go
# LOG_TRIM_BATCH_ROWS at a time. A file bigger than LOG_SHARD_VACUUM_RATIO times its limit is rewritten. A query returns
# the newest LOG_QUERY_MAX_ROWS lines at most.
LOG_SHARD_MAX_BYTES = 256 * 1024 * 1024
LOG_TOTAL_MAX_BYTES = 2 * 1024 * 1024 * 1024
LOG_TOTAL_MAX_DISK_SHARE = 0.10
LOG_TRIM_TO_SHARE = 0.9
LOG_KEEP_NEWEST_ROWS = 1000
LOG_SIZE_CHECK_SECONDS = 30
LOG_TRIM_BATCH_ROWS = 10000
LOG_SHARD_VACUUM_RATIO = 1.25
LOG_QUERY_MAX_ROWS = 1000

# Cloudflare network errors, rate limits and server errors are retried after these delays (seconds).
CLOUDFLARE_RETRY_DELAYS = (1, 2)
# S3 deletes are retried after these delays (seconds).
S3_RETRY_DELAYS = (1, 2)
# A cold start without a certificate retries before giving up (seconds).
STARTUP_RETRY_DELAYS = (60, 240, 960)
RENEWAL_THRESHOLD_DAYS = 30
DNS_PROPAGATION_TIMEOUT = 180
DNS_POLL_INTERVAL = 5
FINALIZE_TIMEOUT_SECONDS = 180

# The manager samples its own usage every few seconds and reports the per-minute peak
METRICS_SAMPLE_INTERVAL = 5

# Telegraf on a worker gathers and pushes a report this often. The port-9001 server keeps an
# idle connection longer than that (uvicorn's 5 s default would close it between reports). A
# report is refused above REPORT_MAX_BYTES once decompressed.
REPORT_INTERVAL_SECONDS = 10
REPORT_KEEP_ALIVE_SECONDS = 30
REPORT_MAX_BYTES = 8 * 1024 * 1024
# A push of logs from a worker's Vector is refused above this; Vector's batches are capped at half of it.
LOG_INTAKE_MAX_BYTES = 8 * 1024 * 1024

# A worker's report is current for this long. A snapshot can stand for how things are now only if
# it arrived this long after the last change the manager made to the rows it is compared with
# (a gather, a flush and a request), since a snapshot taken before that change says nothing about it.
REPORT_STALE_SECONDS = 30
REPORT_SETTLE_SECONDS = 15
# After the manager starts, a worker that has not reported yet is not called not ready for this long.
REPORT_STARTUP_GRACE_SECONDS = 60
# The names of unknown senders remembered so that each is logged once.
REPORT_UNKNOWN_LIMIT = 100
# A worker's newest minute of metrics is shown in the worker list while it began less than this long ago: a minute is
# written by the first minutely flush after it ends, so a worker that reports has one that began at most about 2
# minutes ago.
WORKER_METRICS_STALE_SECONDS = 180

# The services of the worker stack, which `docker compose up -d` starts and which are also the names
# of their containers.
WORKER_STACK_SERVICES = ("traefik", "vector", "telegraf", "cloudflared")

# A worker is offline after this many checks in a row went unanswered (about a minute).
OFFLINE_AFTER_MISSED_CHECKS = 2
# How long to wait for a worker's SSH banner.
SSH_BANNER_TIMEOUT_SECONDS = 3
PING_TIMEOUT_SECONDS = 3

# A reboot that has not finished by now (a worker that never came back) is no longer called one.
WORKER_REBOOT_LIMIT_SECONDS = 300

# A worker whose stack is down (or silent for this long) gets `docker compose up -d`, at most
# once per cooldown.
WORKER_STACK_SILENCE_SECONDS = 60
WORKER_STACK_REPAIR_COOLDOWN_SECONDS = 60

# A command on a worker is wrapped in `timeout`, which stops it at its deadline and, if it
# ignores that, kills it this many seconds later. The manager waits this much longer than the
# deadline before it gives up on a worker that answers nothing.
COMMAND_KILL_AFTER_SECONDS = 5
COMMAND_WAIT_GRACE_SECONDS = 15
# How long to wait for a closed SSH connection to finish closing.
SSH_CLOSE_WAIT_SECONDS = 5

# UI download presigned link expiry
BROWSER_LINK_SECONDS = 60
# A worker's upload or download presigned link expiry
WORKER_LINK_SECONDS = 1200
