import os
import re
import logging
from datetime import datetime
from peewee import CharField, FixedCharField, SqliteDatabase, DateTimeField, BooleanField, ForeignKeyField, IntegerField
from playhouse.signals import Model, post_save, post_delete, pre_save

from services.base import Base
from utils.db import EncryptedTextField, CleanCharField


logger = logging.getLogger(__name__)

DB_PATH = '/app/data/data.db'
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = SqliteDatabase(DB_PATH, pragmas={
  'journal_mode': 'wal',  # Allow readers while writer active.
  'cache_size': -64000,  # 64 MB page cache.
  'foreign_keys': 1,  # Enforce FK constraints.
  'busy_timeout': 5000,  # Wait up to 5s before returning SQLITE_BUSY.
})

STATUS_CHOICES = ['active', 'inactive', 'deploying', 'stopping', 'error']

class BaseModel(Model):
  created_at = DateTimeField(default=datetime.now)
  updated_at = DateTimeField(default=datetime.now)

  class Meta:
    database = db

  def save(self, *args, **kwargs):
    self.updated_at = datetime.now()
    return super().save(*args, **kwargs)

class Setting(BaseModel):
  key   = CleanCharField(primary_key=True)
  value = EncryptedTextField(null=True)

class Worker(BaseModel):
  hostname  = CleanCharField(primary_key=True)
  ip        = FixedCharField(15)
  online    = BooleanField(default=False)

class Project(BaseModel):
  name              = CleanCharField(primary_key=True)
  label             = CharField()
  description       = CharField(null=True)
  env               = EncryptedTextField(null=True)
  application_count = IntegerField(default=0)

class Application(BaseModel):
  project         = ForeignKeyField(Project, backref='applications', on_delete='RESTRICT')
  name            = CleanCharField()
  label           = CharField()
  description     = CharField(null=True)
  type            = CharField(choices=["docker", "git"], default="docker")
  image           = CharField(null=True)
  repo            = CharField(null=True)
  path            = CharField(default="Dockerfile")
  env             = EncryptedTextField(null=True)
  args            = EncryptedTextField(null=True)
  status          = CharField(choices=STATUS_CHOICES, default='inactive')
  domains_synced  = BooleanField(default=False)
  container_count = IntegerField(default=0)

  def save(self, *args, **kwargs):
    if self.type == "docker":
      self.repo = None
      self.path = 'Dockerfile'
      self.args = None
    
    if self.type == "git":
      self.image = None
      if self.repo:
        # Validate Git repo URL (basic check)
        pattern = r'^https?://[\w.-]+/[\w.-]+/[\w.-]+(?:\.git)?(?:#[\w.-]+)?(?::[\w./-]+)?$'
        if not re.match(pattern, self.repo):
          raise ValueError(f"Invalid Git repository URL: {self.repo}. Should be in format 'https://github.com/user/repo<?.git><?#branch><?:sub-directory>'")

    return super().save(*args, **kwargs)

  class Meta:
    indexes = (
      (('project', 'name'), True),
    )

@pre_save(sender=Application)
def set_domains_synced_false_on_update(model_class, instance, created):
  if not created:
    dirty = instance.dirty_fields
    if 'domains_synced' not in [f.name for f in dirty]:
      instance.domains_synced = False

@post_save(sender=Application)
def update_application_count_on_save(model_class, instance, created):
  if created:
    Project.update(application_count=Project.application_count + 1).where(Project.name == instance.project_id).execute()

@post_delete(sender=Application)
def update_application_count_on_delete(model_class, instance):
  Project.update(application_count=Project.application_count - 1).where(Project.name == instance.project_id).execute()

class Domain(BaseModel):
  application = ForeignKeyField(Application, backref='domains', on_delete='CASCADE')
  name        = CleanCharField(null=False)
  type        = CharField(choices=['internal', 'public'], default='internal')
  port        = IntegerField(null=False)

  class Meta:
    indexes = (
      (('application', 'name', 'type'), True),
    )

@post_save(sender=Domain)
def set_domains_synced_false_on_update_on_domain(model_class, instance, created):
  if instance.application:
    instance.application.domains_synced = False
    instance.application.save()

class Container(BaseModel):
  application = ForeignKeyField(Application, backref='containers', on_delete='RESTRICT')
  worker      = ForeignKeyField(Worker, backref='containers')
  status      = CharField(choices=STATUS_CHOICES, default='inactive')

  class Meta:
    indexes = (
      (('application', 'worker'), True),
    )

@post_save(sender=Container)
def update_container_count_on_save(model_class, instance, created):
  if created:
    Application.update(container_count=Application.container_count + 1).where(Application.id == instance.application_id).execute()

@post_delete(sender=Container)
def update_container_count_on_delete(model_class, instance):
  Application.update(container_count=Application.container_count - 1).where(Application.id == instance.application_id).execute()

@post_save(sender=Container)
def set_domains_synced_false_on_update_on_container(model_class, instance, created):
  if instance.application:
    instance.application.domains_synced = False
    instance.application.save()

class Deployment(BaseModel):
  container           = ForeignKeyField(Container, backref='deployments', on_delete='CASCADE')
  type                = CharField(choices=['deploy', 'stop', 'delete'])
  application_task_id = CharField()
  container_task_id   = CharField()

  class Meta:
    indexes = (
      (('container', 'application_task_id', 'container_task_id'), True),
    )

class Database(Base):
  def __init__(self):
    super().__init__()

    db.connect(reuse_if_open=True)
    db.create_tables([Setting, Project, Application, Domain, Worker, Container, Deployment], safe=True)
    logger.info("Connected to database.")