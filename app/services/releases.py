"""The newest release of Sage on GitHub, as last fetched."""
import logging
import re

import httpx

import config
from services.base import Base
from services.updates import Updates, UpdateSource
from utils.common import utcnow

logger = logging.getLogger(__name__)

HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class Releases(Base):
  """The newest release as last fetched and its version, which is the running one until a fetch finds another."""

  def __init__(self):
    super().__init__()
    self.http = httpx.Client(timeout=10)
    self.latest_version = config.VERSION
    self.fetched = None

  def refresh(self) -> dict:
    """Fetches the newest release and keeps it, pushing `release` and, when its version is new, `latest_version`; a
    failed fetch logs a warning and keeps the last. Blocking: offload it."""
    try:
      response = self.http.get(config.LATEST_RELEASE_URL, headers={"Accept": "application/vnd.github+json"})
      response.raise_for_status()
      release = response.json()
    except Exception as e:
      logger.warning(f"Failed to fetch the latest Sage release: {e}")
      return self.latest()
    version = (release.get("tag_name") or "").lstrip("v")
    body = release.get("body")
    with self.lock:
      previous = self.latest_version
      self.latest_version = version or previous
      self.fetched = {
          "version": version,
          "name": release.get("name"),
          "body": HTML_COMMENT_RE.sub("", body).strip() if body else body,
          "html_url": release.get("html_url"),
          "published_at": release.get("published_at"),
          "fetched_at": utcnow().isoformat(),
      }
    Updates().changed(UpdateSource.RELEASE)
    if self.latest_version != previous:
      Updates().changed(UpdateSource.LATEST_VERSION)
    return self.fetched

  def latest(self) -> dict:
    """The newest release as last fetched; before a fetch, only its version is known."""
    return self.fetched or {"version": self.latest_version, "name": None, "body": None, "html_url": None,
                            "published_at": None, "fetched_at": None}
