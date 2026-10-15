import logging
from types import SimpleNamespace

import requests

from services.base import Base
from services.settings import Settings
from utils.common import get_env

logger = logging.getLogger(__name__)

NOTIFICATION_STYLES = {
    "info": {"emoji": "ℹ️", "color": 3447003},
    "success": {"emoji": "✅", "color": 3066993},
    "warning": {"emoji": "⚠️", "color": 15105570},
    "error": {"emoji": "❌", "color": 15158332},
}


_session = requests.Session()


def _dispatch_webhook_with_logging(webhook_url, payload, name):
  """Post a webhook payload; returns why it was rejected, or None. Dispatch is
  fire-and-forget and ignores the result; the config check reports it."""
  try:
    response = _session.post(webhook_url, json=payload, timeout=5)
    response.raise_for_status()
  except Exception as e:
    reason = webhook_error(e, name)
    logger.error(f"Failed to send notification to {name}: {reason}")
    return reason
  return None


def webhook_error(error: Exception, name: str) -> str:
  """Why a webhook request failed, without its URL (which holds the webhook token)."""
  if isinstance(error, requests.HTTPError):
    return f"{name} answered HTTP {error.response.status_code}"
  if isinstance(error, requests.Timeout):
    return f"{name} did not answer in time"
  if isinstance(error, requests.ConnectionError):
    return f"could not reach {name}"
  return f"{name} request failed ({type(error).__name__})"


def _discord_payload(instance, domain):
  style = NOTIFICATION_STYLES.get(instance.type, NOTIFICATION_STYLES["info"])
  embed = {
      "title": f"{style['emoji']} Sage {domain}",
      "description": instance.content,
      "color": style["color"],
  }
  if instance.link:
    embed["fields"] = [{"name": "", "value": instance.link, "inline": False}]
  return {"embeds": [embed]}


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

  def check_config(self, config: dict):
    """Send a test message through every configured channel; raises with the reason a
    channel rejected it. Every channel is optional."""
    test = SimpleNamespace(
        type="success",
        content="Test notification — this channel is configured correctly.",
        link=None,
    )
    domain = get_env("DOMAIN")

    failed = []
    for field, channel in CHANNELS.items():
      if not config.get(field):
        continue
      reason = _dispatch_webhook_with_logging(config[field], channel["payload"](test, domain), channel["name"])
      if reason:
        failed.append(reason)

    if failed:
      raise ValueError(f"The test message was not delivered: {'; '.join(failed)}.")

  def dispatch(self, instance):
    if isinstance(instance, dict):
      instance = SimpleNamespace(type=instance.get("type", "info"),
                                 content=instance["content"],
                                 link=instance.get("link"),)

    domain = get_env("DOMAIN")

    for field, channel in CHANNELS.items():
      webhook_url = self.get_notification_value(field)
      if not webhook_url:
        continue
      try:
        _dispatch_webhook_with_logging(
            webhook_url, channel["payload"](instance, domain), channel["name"])
      except Exception as e:
        logger.error(f"Failed to send notification to {channel['name']}: {e}")
