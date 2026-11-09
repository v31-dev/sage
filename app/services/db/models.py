from peewee import SQL, CharField, Check, DateTimeField, ForeignKeyField, IntegerField, Model, SqliteDatabase

from services.db.fields import (AlphaNumericField, ChoiceField, CleanCharField, EncryptedJSONField, EncryptedKeyValueField, JSONField, LabelField,
                                OriginListField)
from utils.common import new_id, utcnow

# Deferred: opened by Database at startup.
db = SqliteDatabase(None)


class BaseModel(Model):
  created_at = DateTimeField(default=utcnow)
  updated_at = DateTimeField(default=utcnow)

  class Meta:
    database = db
    only_save_dirty = True

  def save(self, *args, **kwargs):
    self.updated_at = utcnow()
    return super().save(*args, **kwargs)


class Setting(BaseModel):
  key = AlphaNumericField(primary_key=True)
  value = EncryptedJSONField(null=True)


class Instance(BaseModel):
  """This manager's identity, made on the first boot and kept in the database, so a restored
  backup is the same instance. A worker records it to say whose it is. `state_generation` names
  the installed configuration: a restored database gets a fresh one before it is installed."""
  id = CharField(primary_key=True, default=new_id)
  state_generation = CharField(default=new_id)


class Worker(BaseModel):
  hostname = CleanCharField(primary_key=True)
  # Its internal names are under `<subdomain>.int.<domain>`.
  subdomain = LabelField()

  class Meta:
    indexes = ((("subdomain",), True),)


class Project(BaseModel):
  name = AlphaNumericField(primary_key=True)
  label = CharField()
  description = CharField(null=True)
  env = EncryptedKeyValueField(null=True)
  application_count = IntegerField(default=0, constraints=[Check("application_count >= 0")])


class Application(BaseModel):
  project = ForeignKeyField(Project, backref="applications", on_delete="RESTRICT")
  name = AlphaNumericField()
  label = CharField()
  description = CharField(null=True)
  type = ChoiceField(("docker", "git"), default="docker")
  image = CharField(null=True)
  repo = CharField(null=True)
  path = CharField(default="Dockerfile")
  env = EncryptedKeyValueField(null=True)
  args = EncryptedKeyValueField(null=True)
  build_secrets = EncryptedKeyValueField(null=True)
  command = CharField(null=True)
  container_count = IntegerField(default=0, constraints=[Check("container_count >= 0")])

  class Meta:
    indexes = ((("project", "name"), True),)


class Container(BaseModel):
  id = CharField(primary_key=True, default=new_id)
  application = ForeignKeyField(Application, backref="containers", on_delete="RESTRICT")
  worker = ForeignKeyField(Worker, backref="containers")

  class Meta:
    indexes = ((("application", "worker"), True),)


class Domain(BaseModel):
  """A name linked to one container, which it routes to on `port`: public `<name>.<domain>`, or internal or TCP
  `<name>.<subdomain>.int.<domain>` under its namespace worker's subdomain, which it keeps when its container is
  unlinked. Deleting the container unlinks it. `service` groups names for discovery, apart for each namespace (public,
  or internal and TCP), and `pool` is a subgroup of a service."""
  id = CharField(primary_key=True, default=new_id)
  name = LabelField()
  type = ChoiceField(("internal", "public", "tcp"), default="internal")
  port = IntegerField(constraints=[Check("port BETWEEN 1 AND 65535")])
  container = ForeignKeyField(Container, backref="domains", null=True, on_delete="SET NULL")
  namespace_worker = ForeignKeyField(Worker, backref="domains", null=True, on_delete="RESTRICT")
  service = LabelField(null=True)
  pool = LabelField(null=True)
  cors_origins = OriginListField(null=True)


# A name once among the public domains, and once per worker among the internal and TCP ones.
Domain.add_index(Domain.index(Domain.name, SQL("coalesce(namespace_worker_id, '')"), unique=True,
                              name="domain_name_namespace_worker"))


class Volume(BaseModel):
  id = CharField(primary_key=True, default=new_id)
  name = AlphaNumericField()
  path = CharField()
  backup_cron = CharField(null=True)
  application = ForeignKeyField(Application, backref="volumes", on_delete="CASCADE")

  class Meta:
    indexes = ((("application", "name"), True),)


EVENT_TRANSITIONS = ("admitted", "rejected", "succeeded", "failed", "interrupted", "recovered", "recovery_failed",
                     "verified", "observed")


class BaseEvent(BaseModel):
  """A step of an operation on a resource, or an observed change of it: history, never what decides what runs.
  Deleting the resource clears its link; `subject_id` and the context beside it keep what it was."""
  subject_id = CharField()
  operation_id = CharField(null=True, index=True)
  transition = ChoiceField(EVENT_TRANSITIONS)
  reason = JSONField(null=True)
  task_id = CharField(null=True)
  part_id = CharField(null=True)
  dedupe_key = CharField(null=True, unique=True)

  class Meta:
    indexes = ((("created_at",), False),)


class ContainerEvent(BaseEvent):
  container = ForeignKeyField(Container, backref="events", null=True, on_delete="SET NULL")
  action = ChoiceField(("deploy", "stop", "delete", "backup_stopped", "backup_preserve", "restore_stopped", "reset"))
  application_name = CharField()
  worker_hostname = CharField()

  class Meta:
    table_name = "container_event"


class WorkerEvent(BaseEvent):
  worker = ForeignKeyField(Worker, backref="events", null=True, on_delete="SET NULL")
  action = ChoiceField(("add", "remove", "configure", "infrastructure", "repair", "reboot", "connectivity", "readiness",
                        "ownership"))

  class Meta:
    table_name = "worker_event"


class Notification(BaseModel):
  type = ChoiceField(("info", "success", "warning", "error"), default="info")
  content = CharField()
  link = CharField(null=True)

  class Meta:
    indexes = ((("created_at",), False),)


class Backup(BaseModel):
  type = ChoiceField(("platform", "application"), default="platform")
  s3_path = CharField()
  # The store it was written to (`S3Session.identity`): a backup is reached only through that store.
  storage_id = CharField()
  # Not a foreign key: a backup outlives its volume.
  source_volume_id = CharField(null=True)
  source_volume_name = CharField(null=True)
  application = ForeignKeyField(Application, backref="backups", null=True, on_delete="CASCADE")


class Task(BaseModel):
  # Log of finished operations from the in-memory queue. Running tasks live in
  # memory only (see the live snapshot); a row is written once a task reaches a
  # terminal state -- completed, failed, or cancelled. `created_at` is when the
  # task was queued.
  task_id = CharField(primary_key=True)
  name = CharField()
  scopes = JSONField(null=True)
  params = JSONField(null=True)
  executor = CharField()
  status = ChoiceField(("completed", "failed", "cancelled"))
  started_at = DateTimeField(null=True)  # none for a task cancelled before it started
  finished_at = DateTimeField(null=True)

  class Meta:
    indexes = ((("created_at",), False), (("finished_at",), False))
