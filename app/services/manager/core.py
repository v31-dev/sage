import logging

from playhouse.shortcuts import model_to_dict

from services.db import Notification
from services.notification import Notifications
from services.updates import UpdateFeed, Updates
from utils.executor import NOTIFICATIONS_EXECUTOR, submit_with_context

logger = logging.getLogger(__name__)


class CoreMixin:
  def notify(self, message: str, type: str = "info", link: str | None = None):
    # Python logging has no success level; treat it as info.
    log_method = logger.info if type == "success" else getattr(logger, type, logger.info)
    log_method(message)
    notification = Notification.create(content=message, type=type, link=link)
    Updates().publish(UpdateFeed.NOTIFICATIONS, [model_to_dict(notification, recurse=False)])
    submit_with_context(NOTIFICATIONS_EXECUTOR, Notifications().dispatch, notification)
