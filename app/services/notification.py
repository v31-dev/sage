import logging
from types import SimpleNamespace

import requests

from services.base import Base
from services.settings import Settings
from utils.common import get_env
from utils.retry import is_transient

logger = logging.getLogger(__name__)

NOTIFICATION_STYLES = {
    "info": {"emoji": "ℹ️", "color": 3447003},
    "success": {"emoji": "✅", "color": 3066993},
    "warning": {"emoji": "⚠️", "color": 15105570},
    "error": {"emoji": "❌", "color": 15158332},
}


_session = requests.Session()


def webhook_failure(error: Exception, name: str) -> tuple[str, bool]:
  """Why a webhook request failed, without its URL (which holds the webhook token), and
  whether that clears by itself."""
  if isinstance(error, requests.HTTPError):
    message = f"{name} answered HTTP {error.response.status_code}"
  elif isinstance(error, requests.Timeout):
    message = f"{name} did not answer in time"
  elif isinstance(error, requests.ConnectionError):
    message = f"could not reach {name}"
  else:
    message = f"{name} request failed ({type(error).__name__})"
  return message, is_transient(error)


def _request_webhook(method: str, url: str, name: str, **kwargs) -> tuple[str, bool] | None:
  """Request a webhook; returns why it failed and whether that clears by itself, or None."""
  try:
    _session.request(method, url, timeout=5, **kwargs).raise_for_status()
  except Exception as e:
    return webhook_failure(e, name)
  return None


class ChannelCheckFailed(ValueError):
  """Some configured channels failed a check. `failures` is {field: (reason, transient)}."""

  def __init__(self, failures: dict[str, tuple[str, bool]], configured: int, prefix: str = ""):
    self.failures = failures
    # Only a section whose every channel failed, each in a way that clears by itself, is
    # retrying. A partial failure needs the user.
    self.transient = len(failures) == configured and all(transient for _, transient in failures.values())
    reasons = "; ".join(reason for reason, _ in failures.values())
    super().__init__(f"{prefix}: {reasons}." if prefix else reasons)

  @property
  def channels(self) -> dict[str, str]:
    """Why each failing channel failed, by its settings field."""
    return {field: reason for field, (reason, _) in self.failures.items()}


def _source() -> str:
  """The manager's name and domain."""
  return f"{get_env('TS_NAME')} {get_env('DOMAIN')}"


def _discord_payload(instance, source):
  style = NOTIFICATION_STYLES.get(instance.type, NOTIFICATION_STYLES["info"])
  embed = {
      "title": f"{style['emoji']} {source}",
      "description": instance.content,
      "color": style["color"],
  }
  if instance.link:
    embed["fields"] = [{"name": "", "value": instance.link, "inline": False}]
  return {"embeds": [embed]}


TEST_NOTIFICATION = SimpleNamespace(
    type="success",
    content="Test notification — this channel is configured correctly.",
    link=None,
)

# One row per delivery channel, keyed by its `notifications` settings field
CHANNELS = {
    "discord_webhook": {"name": "Discord", "payload": _discord_payload},
}


class Notifications(Base):
  def __init__(self):
    super().__init__()
    self.notifications_config = {}
    self.load_notifications_config()

  def load_notifications_config(self):
    with self.lock:
      self.notifications_config = Settings().get("notifications")

  def get_notification_value(self, key: str, default=None):
    with self.lock:
      return self.notifications_config.get(key, default)

  def check_config(self, config: dict, send: bool = True) -> list[str]:
    """Check every configured channel (each is optional) and return their names. Raises
    ChannelCheckFailed naming the ones that failed. With send, each gets a test message;
    without, its webhook is only read, which posts nothing to the channel."""
    source = _source()
    configured = {field: channel for field, channel in CHANNELS.items() if config.get(field)}

    failures = {}
    for field, channel in configured.items():
      if send:
        failure = _request_webhook("POST", config[field], channel["name"],
                                   json=channel["payload"](TEST_NOTIFICATION, source))
      else:
        failure = _request_webhook("GET", config[field], channel["name"])
      if failure:
        failures[field] = failure

    if failures:
      raise ChannelCheckFailed(
          failures, len(configured), "The test message was not delivered" if send else "")
    return [channel["name"] for channel in configured.values()]

  def dispatch(self, instance):
    if isinstance(instance, dict):
      instance = SimpleNamespace(type=instance.get("type", "info"),
                                 content=instance["content"],
                                 link=instance.get("link"),)

    source = _source()

    for field, channel in CHANNELS.items():
      webhook_url = self.get_notification_value(field)
      if not webhook_url:
        continue
      try:
        failure = _request_webhook(
            "POST", webhook_url, channel["name"], json=channel["payload"](instance, source))
        if failure:
          logger.error(f"Failed to send notification to {channel['name']}: {failure[0]}")
      except Exception as e:
        logger.error(f"Failed to send notification to {channel['name']}: {e}")
