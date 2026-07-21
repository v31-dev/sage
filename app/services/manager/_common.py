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


# Worker infra files, pushed by `setup_worker` behind its `infra` stamp.
WORKER_INFRA_TEMPLATES = templates_digest(
    "worker/docker-compose.yml",
    "worker/worker.env",
    "worker/traefik/traefik.yml",
    "worker/traefik/config.yml",
    "worker/traefik/certs.yml",
    "worker/vector/vector.yml",
)

# Per-application routing files, pushed by `sync_application_traefik_domains_config`
# behind each application's own stamp.
APPLICATION_ROUTING_TEMPLATES = templates_digest(
    "worker/traefik/service_public.yml",
    "worker/traefik/service_public_pool.yml",
    "worker/traefik/service_internal.yml",
    "worker/traefik/service_internal_pool.yml",
    "worker/traefik/service_internal_tcp.yml",
)


def routing_input_hash(domain_name: str, domains, containers) -> str:
  """Hash of everything that determines an application's rendered Traefik
  files on any single worker. The receiving-worker set is deliberately
  excluded: a worker that missed a sync is repaired through its own stamp,
  and one worker going offline must not invalidate the others' stamps."""
  return content_hash({
      "templates": APPLICATION_ROUTING_TEMPLATES,
      "domain": domain_name,
      "domains": sorted((d.name, d.type, d.port) for d in domains),
      "tags": sorted({c.domain_tag for c in containers if c.domain_tag}),
      "active": sorted(
          (c.worker.hostname, c.worker.ip, c.domain_tag or "")
          for c in containers
          if c.worker.online and c.status == "active"
      ),
  })
