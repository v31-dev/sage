import hashlib
import json
from pathlib import Path

# This module lives at app/services/manager/_common.py, so three .parent hops
# reach the app/ root. Defined here once so every mixin shares the same path.
app_dir = Path(__file__).parent.parent.parent


def content_hash(payload) -> str:
  return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


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


# The discovery files, pushed by `sync_discovery` behind each worker's `discovery` stamp.
DISCOVERY_TEMPLATES = templates_digest(
    "worker/traefik/discovery_public.yml",
    "worker/traefik/discovery_internal.yml",
)

CERTIFICATE_TEMPLATES = templates_digest("worker/traefik/certs.yml")
