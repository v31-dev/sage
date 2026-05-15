import logging

from services.base import Base
from .models import (
    Application,
    Backup,
    Container,
    Domain,
    Event,
    Notification,
    Project,
    Setting,
    Volume,
    Worker,
    db,
)

logger = logging.getLogger(__name__)


class Database(Base):
  def __init__(self):
    super().__init__()

    db.connect(reuse_if_open=True)

    db.create_tables(
        [Setting, Project, Application, Volume, Domain, Worker, Container, Event, Notification, Backup],
        safe=True,
    )

    logger.info("Connected to database.")
