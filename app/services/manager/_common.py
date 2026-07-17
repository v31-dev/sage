import hashlib
import json
from pathlib import Path

# This module lives at app/services/manager/_common.py, so three .parent hops
# reach the app/ root. Defined here once so every mixin shares the same path.
app_dir = Path(__file__).parent.parent.parent


def content_hash(payload) -> str:
  return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def routing_input_hash(domain_name: str, domains, containers) -> str:
  """Hash of everything that determines an application's rendered Traefik
  files on any single worker. The receiving-worker set is deliberately
  excluded: a worker that missed a sync is repaired through its own stamp,
  and one worker going offline must not invalidate the others' stamps."""
  return content_hash({
      "domain": domain_name,
      "domains": sorted((d.name, d.type, d.port) for d in domains),
      "tags": sorted({c.domain_tag for c in containers if c.domain_tag}),
      "active": sorted(
          (c.worker.hostname, c.worker.ip, c.domain_tag or "")
          for c in containers
          if c.worker.online and c.status == "active"
      ),
  })
