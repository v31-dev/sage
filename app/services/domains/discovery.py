"""What discovery publishes: each service's names with their pools and whether each can be used, the file each worker
serves a service from, and the fingerprint the worker's `discovery` stamp holds."""
import hashlib
import json

import config
from utils.common import fill

TEMPLATES = {"public": "discovery_public.yml", "internal": "discovery_internal.yml"}
# The discovery templates' digest, part of every fingerprint: they ship in the image, so it is worked out once.
TEMPLATES_DIGEST = hashlib.sha256(b"".join(
    name.encode() + (config.TEMPLATES_DIR / "worker/traefik" / name).read_bytes()
    for name in sorted(TEMPLATES.values()))).hexdigest()[:16]
PREFIX, SUFFIX = "_discovery-", ".yml"


def discovery_services(domains) -> list:
  """Services as `[namespace, service, [[host, pool, online], ...]]`, sorted, from `(domain, state)` of each Domain
  with a service: public, or internal for internal and TCP names. Every name with a full name is listed; it is online
  only while its Domain is synced for its current configuration, its container can be routed to and its worker is
  ready."""
  services = {}
  for domain, state in domains:
    if state.config.host is None:
      continue
    online = state.view.status == "synced" and state.facts.eligible is True and state.facts.worker_ready is True
    services.setdefault(("public" if domain.type == "public" else "internal", domain.service), []).append(
        [state.config.host, domain.pool, online])
  return [[namespace, service, sorted(names, key=lambda name: name[0])] for (namespace, service), names in sorted(services.items())]


def discovery_fingerprint(services: list, base_domain: str) -> str:
  return hashlib.sha256(json.dumps({"templates": TEMPLATES_DIGEST, "domain": base_domain, "services": services,
                                    "cache_seconds": config.DISCOVERY_CACHE_SECONDS}, sort_keys=True).encode()).hexdigest()[:16]


def discovery_files(services: list, base_domain: str) -> dict[str, str]:
  """Each service's file by name: a router on `discovery.<domain>` (public) or `discovery.int.<domain>` (internal)
  answering `?service=<service>` with its names, pools and availability in `Sage-Endpoints`."""
  hosts = {"public": f"discovery.{base_domain}", "internal": f"discovery.int.{base_domain}"}
  return {f"{PREFIX}{namespace}-{service}{SUFFIX}": fill(
      (config.TEMPLATES_DIR / "worker/traefik" / TEMPLATES[namespace]).read_text(), {
          "SERVICE": service, "HOST": hosts[namespace], "CACHE_SECONDS": config.DISCOVERY_CACHE_SECONDS,
          "ENDPOINTS": ", ".join(f'"{host}"' + (f';pool="{pool}"' if pool else "") + (";online" if online else ";online=?0")
                                 for host, pool, online in names)})
      for namespace, service, names in services}


def files_command(dynamic: str) -> str:
  """Lists the names of the discovery files in the worker's dynamic directory, one per line."""
  return f"find {dynamic} -maxdepth 1 -name '{PREFIX}*{SUFFIX}' -printf '%f\\n' 2>/dev/null || true"
