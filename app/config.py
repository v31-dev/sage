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

# Cloudflare network errors, rate limits and server errors are retried after these delays (seconds).
CLOUDFLARE_RETRY_DELAYS = (1, 2)
# A cold start without a certificate retries before giving up (seconds).
STARTUP_RETRY_DELAYS = (60, 240, 960)
RENEWAL_THRESHOLD_DAYS = 30
DNS_PROPAGATION_TIMEOUT = 180
DNS_POLL_INTERVAL = 5
FINALIZE_TIMEOUT_SECONDS = 180

# The manager samples its own usage every few seconds and reports the per-minute peak
METRICS_SAMPLE_INTERVAL = 5

# UI download presigned link expiry
BROWSER_LINK_SECONDS = 60
# A worker's upload or download presigned link expiry
WORKER_LINK_SECONDS = 1200
