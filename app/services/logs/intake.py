"""A push from a worker's Vector, split between the requests and the applications."""
import json
import logging
from functools import partial

from .requests import REQUESTS, TRAEFIK, access_line
from .store import stored_time

logger = logging.getLogger(__name__)


class IntakeMixin:
  def ingest(self, hostname: str, body: bytes) -> int:
    """A push from a worker's Vector, one event a line: Traefik's access lines to the requests, and each application's
    lines to its shard, one write per file. Traefik's own lines are dropped. Returns how many lines were skipped (not
    an event, or of a container whose name cannot be a file); raises after the other writes when one fails."""
    by_container: dict[str, list] = {}
    requests = []
    skipped = 0
    for line in body.splitlines():
      if not line.strip():
        continue
      try:
        event = json.loads(line)
        container, ts, message = event["container_name"], stored_time(event["timestamp"]), event["message"]
      except (ValueError, KeyError, TypeError):
        skipped += 1
        continue
      if container == TRAEFIK:
        if (request := access_line(hostname, message)) is not None:
          requests.append(request)
      else:
        by_container.setdefault(container, []).append(
            {"hostname": hostname, "ts": ts, "stream": event.get("stream", ""), "message": message})

    failed = None
    writes = [(REQUESTS, requests, self.write_requests)] if requests else []
    writes += [(container, rows, partial(self.write_application_lines, container))
               for container, rows in by_container.items()]
    for name, rows, write in writes:
      try:
        write(rows)
      except ValueError:
        skipped += len(rows)
      except Exception as e:
        logger.warning(f"Could not write {len(rows)} log lines of '{name}': {e}")
        failed = failed or e
    if failed is not None:
      raise failed
    return skipped
