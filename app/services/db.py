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

  class Meta:
    database = db

  def save(self, *args, **kwargs):
    self.updated_at = datetime.now()
    return super().save(*args, **kwargs)

  @classmethod
  def get_local_fields(cls):
    """
    Returns field names defined in the subclass, 
    excluding inherited fields from BaseModel and the auto 'id'.
    """
    base_names = set(BaseModel._meta.fields.keys())
    ignore = base_names | {'id'}
    return [name for name in cls._meta.fields.keys() if name not in ignore]

class Setting(BaseModel):
  key   = CharField(primary_key=True)
  value = EncryptedTextField(null=True)
  
class Project(BaseModel):
  name        = CharField(primary_key=True)
  description = CharField(null=True)
  env         = EncryptedTextField(null=True)

class Application(BaseModel):
  project     = ForeignKeyField(Project, backref='applications')
  name        = CharField()
  description = CharField(null=True)
  repo        = CharField(null=True)
  path        = CharField(null=True)
  env         = EncryptedTextField(null=True)
  args        = EncryptedTextField(null=True)

  class Meta:
    indexes = (
      (('project', 'name'), True),
    )

class Worker(BaseModel):
  hostname  = CharField(primary_key=True)
  ip        = FixedCharField(15)
  online    = BooleanField(default=False)

class Container(BaseModel):
  project     = ForeignKeyField(Project, backref='containers')
  application = ForeignKeyField(Application, backref='containers')
  worker      = ForeignKeyField(Worker, backref='containers')
  status      = CharField(default='inactive')

  class Meta:
    indexes = (
      (('project', 'application', 'worker'), True),
    )

class Deployment(BaseModel):
  container = ForeignKeyField(Container, backref='deployments')
  task_id   = CharField()

  class Meta:
    indexes = (
      (('container', 'task_id'), True),
    )

class Database(Base):
  def __init__(self):
    super().__init__()

    db.connect(reuse_if_open=True)
    db.create_tables([Setting, Project, Application, Worker, Container, Deployment], safe=True)
    logger.info("Connected to database.")