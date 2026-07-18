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
  try:
    response = _session.post(webhook_url, json=payload, timeout=5)
    response.raise_for_status()
  except Exception as e:
    logger.error(f"Failed to send notification to {name}: {e}")


class Notifications(Base):
  def __init__(self):
    super().__init__()
    self.notifications_config = {}
    self.load_notifications_config()

  def load_notifications_config(self):
    with self.lock:
      self.notifications_config = Settings().get("notifications")

  def get_notifications_config(self):
    with self.lock:
      return dict(self.notifications_config)

  def get_notification_value(self, key: str, default=None):
    with self.lock:
      return self.notifications_config.get(key, default)

  def dispatch(self, instance):
    if isinstance(instance, dict):
      instance = SimpleNamespace(type=instance.get("type", "info"),
                                 content=instance["content"],
                                 link=instance.get("link"),)

    if self.get_notification_value("discord_webhook"):
      self.send_discord(instance)

  def send_discord(self, instance):
    try:
      discord_webhook = self.get_notification_value("discord_webhook")
      if not discord_webhook:
        return

      config = NOTIFICATION_STYLES.get(instance.type, NOTIFICATION_STYLES["info"])
      domain = Settings().get("cloudflare", "domain", "")

      embed = {
          "title": f"{config['emoji']} Sage {domain}",
          "description": instance.content,
          "color": config["color"]
      }
      if instance.link:
        embed["fields"] = [
            {
                "name": "",
                "value": instance.link,
                "inline": False
            }
        ]
      _dispatch_webhook_with_logging(discord_webhook, {"embeds": [embed]}, "Discord")
    except Exception as e:
      logger.error(f"Failed to send notification to Discord: {e}")
