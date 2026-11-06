import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from services.domains import ROUTING_TEMPLATES

# This module lives at app/services/manager/_common.py, so three .parent hops
# reach the app/ root. Defined here once so every mixin shares the same path.
app_dir = Path(__file__).parent.parent.parent

BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


def content_hash(payload) -> str:
  return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def timestamp_from_key(key: str) -> str | None:
  match = re.search(r"\d{8}_\d{6}", key.strip("/").split("/")[-1])
  return match.group(0) if match else None


def is_expired_timestamp(timestamp_str: str, cutoff: datetime) -> bool:
  try:
    return datetime.strptime(timestamp_str, BACKUP_TIMESTAMP_FORMAT) < cutoff
  except Exception:
    return False


def is_expired_backup_key(key: str, cutoff: datetime) -> bool:
  timestamp_str = timestamp_from_key(key)
  return bool(timestamp_str) and is_expired_timestamp(timestamp_str, cutoff)


def templates_digest(*relative_paths: str) -> str:
  """Digest of a set of worker template files. Templates ship inside the image
  and cannot change under a running manager, so the digest is computed once at
  import and folded into the revision hashes below — without it, editing a
  template produces no drift signal and workers keep the old file until an
  unrelated input happens to change."""
  digest = hashlib.sha256()
  for relative_path in sorted(relative_paths):
    digest.update(relative_path.encode())
    digest.update((app_dir / "templates" / relative_path).read_bytes())
  return digest.hexdigest()[:16]


# Worker infra files, pushed by `setup_worker` behind its `infra` stamp.
WORKER_INFRA_TEMPLATES = templates_digest(
    "worker/docker-compose.yml",
    "worker/worker.env",
    "worker/traefik/traefik.yml",
    "worker/traefik/config.yml",
    "worker/vector/vector.yml",
    "worker/telegraf/telegraf.conf",
)

DOMAIN_ROUTING_TEMPLATES = templates_digest(*(f"worker/traefik/{template}" for template in ROUTING_TEMPLATES.values()))


# The discovery files, pushed by `sync_discovery` behind each worker's `discovery` stamp.
DISCOVERY_TEMPLATES = templates_digest(
    "worker/traefik/discovery_public.yml",
    "worker/traefik/discovery_internal.yml",
)

CERTIFICATE_TEMPLATES = templates_digest("worker/traefik/certs.yml")
