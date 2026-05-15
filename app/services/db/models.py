import os
import re
from datetime import datetime

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    FixedCharField,
    ForeignKeyField,
    IntegerField,
    SqliteDatabase,
)
from playhouse.signals import Model

from utils.db import CleanCharField, EncryptedJSONField, EncryptedTextField

DB_PATH = "/app/data/data.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = SqliteDatabase(
    DB_PATH,
    pragmas={
        "journal_mode": "wal",  # Allow readers while writer active.
        "cache_size": -64000,  # 64 MB page cache.
        "foreign_keys": 1,  # Enforce FK constraints.
        "busy_timeout": 5000,  # Wait up to 5s before returning SQLITE_BUSY.
    },
)

STATUS_CHOICES = ["active", "inactive", "deploying", "stopping", "error", "backup", "restoring"]
APPLICATION_BUSY_STATUSES = {"deploying", "stopping", "backup", "restoring"}
APPLICATION_STOP_ELIGIBLE_STATUSES = {"active", "error"}


class BaseModel(Model):
  created_at = DateTimeField(default=datetime.now)
  updated_at = DateTimeField(default=datetime.now)

  class Meta:
    database = db

  def save(self, *args, **kwargs):
    self.updated_at = datetime.now()
    return super().save(*args, **kwargs)


class Setting(BaseModel):
  key = CleanCharField(primary_key=True)
  value = EncryptedJSONField(null=True)


class Worker(BaseModel):
  hostname = CleanCharField(primary_key=True)
  ip = FixedCharField(15)
  online = BooleanField(default=False)


class Project(BaseModel):
  name = CleanCharField(primary_key=True)
  label = CharField()
  description = CharField(null=True)
  env = EncryptedTextField(null=True)
  application_count = IntegerField(default=0)


class Application(BaseModel):
  project = ForeignKeyField(Project, backref="applications", on_delete="RESTRICT")
  name = CleanCharField()
  label = CharField()
  description = CharField(null=True)
  type = CharField(choices=["docker", "git"], default="docker")
  image = CharField(null=True)
  repo = CharField(null=True)
  path = CharField(default="Dockerfile")
  env = EncryptedTextField(null=True)
  args = EncryptedTextField(null=True)
  status = CharField(choices=STATUS_CHOICES, default="inactive")
  domains_synced = BooleanField(default=False)
  container_count = IntegerField(default=0)

  def save(self, *args, **kwargs):
    if self.type == "docker":
      self.repo = None
      self.path = "Dockerfile"
      self.args = None

    if self.type == "git":
      self.image = None
      if self.repo:
        pattern = r"^https?://[\w.-]+/[\w.-]+/[\w.-]+(?:\.git)?(?:#[\w.-]+)?(?::[\w./-]+)?$"
        if not re.match(pattern, self.repo):
          raise ValueError(
              f"Invalid Git repository URL: {
                  self.repo}. Should be in format 'https://github.com/user/repo<?.git><?#branch><?:sub-directory>'")

    return super().save(*args, **kwargs)

  class Meta:
    indexes = ((("project", "name"), True),)


class Domain(BaseModel):
  application = ForeignKeyField(Application, backref="domains", on_delete="CASCADE")
  name = CleanCharField(null=False)
  type = CharField(choices=["internal", "public"], default="internal")
  port = IntegerField(null=False)

  class Meta:
    indexes = ((("application", "name", "type"), True),)


class Container(BaseModel):
  application = ForeignKeyField(Application, backref="containers", on_delete="RESTRICT")
  worker = ForeignKeyField(Worker, backref="containers")
  status = CharField(choices=STATUS_CHOICES, default="inactive")
  domain_tag = CleanCharField(null=True)

  class Meta:
    indexes = ((("application", "worker"), True),)


class Volume(BaseModel):
  name = CleanCharField()
  path = CharField()
  backup_cron = CharField(null=True)
  application = ForeignKeyField(Application, backref="volumes", on_delete="CASCADE")

  class Meta:
    indexes = ((("application", "name"), True),)


class Event(BaseModel):
  container = ForeignKeyField(Container, backref="events", on_delete="CASCADE")
  type = CharField(choices=["deploy", "stop", "delete", "backup", "restore"])
  application_task_id = CharField()
  container_task_id = CharField()

  class Meta:
    indexes = ((("container", "application_task_id", "container_task_id"), True),)


class Notification(BaseModel):
  type = CharField(choices=["info", "success", "warning", "error"], default="info")
  content = CharField()
  link = CharField(null=True)

  class Meta:
    indexes = ((("created_at",), False),)


class Backup(BaseModel):
  type = CharField(choices=["platform", "application"], default="platform")
  s3_path = CharField()
  source_volume_name = CharField(null=True)
  application = ForeignKeyField(Application, backref="backups", null=True, on_delete="CASCADE")