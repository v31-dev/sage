"""What discovery publishes: each service's names with their pools and whether each can be used, and the file each
worker serves a service from, whose first line names the fingerprint it was rendered from."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

import config
from utils.common import fill

TEMPLATES = {"public": "discovery_public.yml", "internal": "discovery_internal.yml"}
# The discovery templates' digest, part of every fingerprint: they ship in the image, so it is worked out once.
TEMPLATES_DIGEST = hashlib.sha256(b"".join(
    name.encode() + (config.TEMPLATES_DIR / "worker/traefik" / name).read_bytes()
    for name in sorted(TEMPLATES.values()))).hexdigest()[:16]
PREFIX, SUFFIX = "_discovery-", ".yml"
HEADER = "# sage-discovery"
ENDPOINTS = "Sage-Endpoints"


@dataclass(frozen=True)
class Published:
  """A discovery file on a worker: the fingerprint its first line names (None when that is not its header) and the
  availability its `Sage-Endpoints` gives each name."""
  proof: str | None
  online: dict[str, bool]


def discovery_services(domains, published: dict[str, bool], now: datetime, started_at: datetime) -> list:
  """Services as `[namespace, service, [[host, pool, online], ...]]`, sorted, from `(domain, state)` of each Domain
  with a service: public, or internal for internal and TCP names. Every name with a full name is listed. `online` is
  true while its Domain is synced, its container can be routed to and its worker is ready; false once one of those is
  known not to hold (unlinked, not routable, not ready, its Domain in error or drifted). While that is not known yet,
  an apply of its Domain under way included, it is what the worker publishes for it (`published`, false when it
  publishes nothing), and false once it has not been known for UNKNOWN_HOLD_SECONDS: from when that apply was queued,
  else from when the Manager started."""
  services = {}
  for domain, state in domains:
    host = state.config.host
    if host is None:
      continue
    facts, view = state.facts, state.view
    if (not facts.linked or facts.eligible is False or facts.worker_ready is False or view.status == "error"
            or (view.status == "pending" and view.reason == "drifted")):
      online = False
    elif view.status == "synced" and facts.eligible is True and facts.worker_ready is True:
      online = True
    else:
      held = (now - (facts.syncing_since or started_at)).total_seconds() < config.UNKNOWN_HOLD_SECONDS
      online = held and published.get(host, False)
    services.setdefault(("public" if domain.type == "public" else "internal", domain.service), []).append(
        [host, domain.pool, online])
  return [[namespace, service, sorted(names, key=lambda name: name[0])] for (namespace, service), names in sorted(services.items())]


def discovery_files(services: list, base_domain: str) -> dict[str, tuple[str, str]]:
  """Each service's file by name, as its fingerprint and its content: a router on `discovery.<domain>` (public) or
  `discovery.int.<domain>` (internal) answering `?service=<service>` with its names, pools and availability in
  `Sage-Endpoints`, its first line naming the fingerprint of the names with their availability, the domain, the
  templates and the cache time."""
  hosts = {"public": f"discovery.{base_domain}", "internal": f"discovery.int.{base_domain}"}
  files = {}
  for namespace, service, names in services:
    fingerprint = hashlib.sha256(json.dumps({
        "templates": TEMPLATES_DIGEST, "domain": base_domain, "service": [namespace, service, names],
        "cache_seconds": config.DISCOVERY_CACHE_SECONDS}, sort_keys=True).encode()).hexdigest()[:16]
    files[f"{PREFIX}{namespace}-{service}{SUFFIX}"] = (fingerprint, fill(
        (config.TEMPLATES_DIR / "worker/traefik" / TEMPLATES[namespace]).read_text(), {
            "FINGERPRINT": fingerprint, "SERVICE": service, "HOST": hosts[namespace], "CACHE_SECONDS": config.DISCOVERY_CACHE_SECONDS,
            "ENDPOINTS": ", ".join(f'"{host}"' + (f';pool="{pool}"' if pool else "") + (";online" if online else ";online=?0")
                                   for host, pool, online in names)}))
  return files


def files_command(dynamic: str) -> str:
  """Lists the first line and the `Sage-Endpoints` line of every discovery file in the worker's dynamic directory for
  `parse_file_listing`."""
  files = f"{dynamic}/{PREFIX}*{SUFFIX}"
  return f"grep -H -m1 '' {files} 2>/dev/null; grep -H '{ENDPOINTS}: ' {files} 2>/dev/null; true"


def parse_file_listing(lines: list[str]) -> dict[str, Published]:
  """Each discovery file `files_command` listed, by name: the fingerprint its header names and each name's
  availability."""
  files = {}
  for line in lines:
    path, separator, content = line.partition(":")
    name = path.rsplit("/", 1)[-1]
    if not (separator and name.startswith(PREFIX) and name.endswith(SUFFIX)):
      continue
    proof, online = (files[name].proof, files[name].online) if name in files else (None, {})
    if endpoints := re.search(rf"{ENDPOINTS}: '(.*)'\s*$", content):
      for item in endpoints[1].split(", "):
        if match := re.fullmatch(r'"([^"]+)"((?:;[^;]+)*)', item):
          params = match[2].split(";")[1:]
          if "online" in params or "online=?0" in params:
            online = {**online, match[1]: "online" in params}
    elif header := re.fullmatch(rf"{re.escape(HEADER)} ([0-9a-f]{{16}})", content):
      proof = header[1]
    files[name] = Published(proof, online)
  return files
