import os
import logging
from datetime import datetime
from peewee import CharField, FixedCharField, SqliteDatabase, Model, DateTimeField, BooleanField, ForeignKeyField 

from services.base import Base
from utils.db import EncryptedTextField


logger = logging.getLogger(__name__)

DB_PATH = '/app/data/data.db'
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = SqliteDatabase(DB_PATH, pragmas={
  'journal_mode': 'wal',  # Allow readers while writer active.
  'cache_size': -64000,  # 64 MB page cache.
  'foreign_keys': 1,  # Enforce FK constraints.
  'busy_timeout': 5000,  # Wait up to 5s before returning SQLITE_BUSY.
})

class BaseModel(Model):
  created_at = DateTimeField(default=datetime.now)
  updated_at = DateTimeField(default=datetime.now)

  def save(self, *args, **kwargs):
    self.updated_at = datetime.now()
    return super().save(*args, **kwargs)

  class Meta:
    database = db

class Setting(BaseModel):
  key   = CharField(primary_key=True)
  value = EncryptedTextField(null=True)
  
class Project(BaseModel):
  name        = CharField(primary_key=True)
  description = CharField(null=True)
  env         = EncryptedTextField(null=True)

class Application(BaseModel):
  name            = CharField(primary_key=True)
  project         = ForeignKeyField(Project, backref='applications')
  description     = CharField(null=True)
  git_repo        = CharField(null=True)
  env             = EncryptedTextField(null=True)
  build_args      = EncryptedTextField(null=True)
  public_domain   = CharField(null=True)
  internal_domain = CharField(null=True)

class Worker(BaseModel):
  hostname  = CharField(primary_key=True)
  ip        = FixedCharField(15)
  online    = BooleanField(default=False)

class Container(BaseModel):
  application = ForeignKeyField(Application, backref='instances')
  worker      = ForeignKeyField(Worker, backref='containers')
  status      = CharField()

  class Meta:
    indexes = (
      (('application', 'worker'), True),
    )

class Database(Base):
  def __init__(self):
    super().__init__()

    db.connect(reuse_if_open=True)
    db.create_tables([Setting, Project, Application, Worker, Container], safe=True)
    logger.info("Connected to database.")