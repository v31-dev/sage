import logging

from playhouse.migrate import SqliteMigrator, migrate

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
    Task,
    Volume,
    Worker,
    db,
)

logger = logging.getLogger(__name__)

MODELS = [Setting, Project, Application, Volume, Domain, Worker, Container, Event, Notification, Backup, Task]


class Database(Base):
  def __init__(self):
    super().__init__()

    db.connect(reuse_if_open=True)

    self.reconcile_schema()

    logger.info("Connected to database.")

  def reconcile_schema(self):
    """
    Bring the live database schema up to the current models: create missing
    tables and add missing columns. Idempotent and safe to call any time the
    on-disk schema may lag the models -- at startup, or after a restore swaps in
    an older database snapshot.
    """
    db.create_tables(MODELS, safe=True)
    self._sync_columns()

  def _sync_columns(self):
    """
    Add any model field that is missing from its table as a new column.

    This is a dynamic, model-driven sync derived from the field definitions
    themselves, not hand-written per-column steps. Anything that cannot be
    applied (e.g. a not-null column without a default on a populated table)
    raises and crashes startup, which is the intended outcome.
    """
    migrator = SqliteMigrator(db)
    for model in MODELS:
      table = model._meta.table_name
      existing = {column.name for column in db.get_columns(table)}
      for field in model._meta.sorted_fields:
        if field.column_name in existing:
          continue
        logger.info(f"Adding missing column {table}.{field.column_name}.")
        migrate(migrator.add_column(table, field.column_name, field))
