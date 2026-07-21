import logging
from types import SimpleNamespace

import requests

from services.base import Base
from services.settings import Settings

logger = logging.getLogger(__name__)

NOTIFICATION_STYLES = {
    "info": {"emoji": "ℹ️", "color": 3447003},
    "success": {"emoji": "✅", "color": 3066993},
    "warning": {"emoji": "⚠️", "color": 15105570},
    "error": {"emoji": "❌", "color": 15158332},
}


_session = requests.Session()


def _dispatch_webhook_with_logging(webhook_url, payload, name):
  """Post a webhook payload, reporting whether it was accepted. Dispatch is
  fire-and-forget and ignores the result; config validation needs it."""
  try:
    response = _session.post(webhook_url, json=payload, timeout=5)
    response.raise_for_status()
  except Exception as e:
    logger.error(f"Failed to send notification to {name}: {e}")
    return False
  return True


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
    """Validate a notifications config. Every channel is optional."""
    test = SimpleNamespace(
        type="success",
        content="Test notification — this channel is configured correctly.",
        link=None,
    )
    domain = Settings().get("cloudflare", "domain", "")

    # Every configured channel is tested
    failed = []
    for field, channel in CHANNELS.items():
      if not config.get(field):
        continue
      if not _dispatch_webhook_with_logging(
              config[field], channel["payload"](test, domain), channel["name"]):
        failed.append(channel["name"])

    if failed:
      logger.warning(f"Notification channels rejected the test message: {', '.join(failed)}")
    return not failed

  def dispatch(self, instance):
    if isinstance(instance, dict):
      instance = SimpleNamespace(type=instance.get("type", "info"),
                                 content=instance["content"],
                                 link=instance.get("link"),)

    domain = Settings().get("cloudflare", "domain", "")

    for field, channel in CHANNELS.items():
      webhook_url = self.get_notification_value(field)
      if not webhook_url:
        continue
      try:
        _dispatch_webhook_with_logging(
            webhook_url, channel["payload"](instance, domain), channel["name"])
      except Exception as e:
        logger.error(f"Failed to send notification to {channel['name']}: {e}")
