import hashlib
import json
import re
import secrets
import string
from dataclasses import dataclass

from peewee import SQL, BooleanField, CharField, DateTimeField, FixedCharField, ForeignKeyField, IntegerField, SqliteDatabase
from playhouse.signals import Model

from services.db.fields import AlphaNumericField, CleanCharField, EncryptedJSONField, EncryptedTextField, JSONField, LabelField, OriginListField
from utils.common import utcnow
from values.key_values import validate_multiline_kv

# Deferred: opened by Database at startup.
db = SqliteDatabase(None)

STATUS_CHOICES = ["active", "inactive", "deploying", "stopping", "error", "backup", "restoring"]
APPLICATION_BUSY_STATUSES = {"deploying", "stopping", "backup", "restoring"}
APPLICATION_STOP_ELIGIBLE_STATUSES = {"active", "error"}


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
  backup is the same instance. A worker records it to say whose it is."""
  id = CharField(primary_key=True, default=lambda: secrets.token_hex(8))


class Worker(BaseModel):
  hostname = CleanCharField(primary_key=True)
  ip = FixedCharField(15)
  # Its internal names are under `<subdomain>.int.<domain>`.
  subdomain = LabelField(null=True)

  @staticmethod
  def random_subdomain() -> str:
    """A subdomain no worker has: a letter and five letters or digits."""
    while True:
      subdomain = secrets.choice(string.ascii_lowercase) + "".join(
          secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))
      if not Worker.select().where(Worker.subdomain == subdomain).exists():
        return subdomain

  class Meta:
    indexes = ((("subdomain",), True),)


class Project(BaseModel):
  name = AlphaNumericField(primary_key=True)
  label = CharField()
  description = CharField(null=True)
  env = EncryptedTextField(null=True)
  application_count = IntegerField(default=0)

  def save(self, *args, **kwargs):
    if self.env:
      validate_multiline_kv(self.env, "project.env")

    return super().save(*args, **kwargs)


@dataclass(frozen=True)
class DeployConfig:
  """Immutable snapshot of an application's deploy inputs, captured once at task
  start so a deploy reads a single point-in-time view while direct UI edits stay
  instant. `content_hash` covers only the code-deployment inputs (build, env,
  volumes); it drives the "configuration changed since last deploy" state and so
  excludes `deploy_stamp` (a fresh value every deploy) and the immutable
  `qualified_name`."""
  qualified_name: str
  deploy_stamp: str | None
  type: str
  image: str | None
  repo: str | None
  path: str
  env: str
  args: str
  build_secrets: str
  command: str
  project_env: str
  volumes: tuple[tuple[str, str], ...]

  @property
  def content_hash(self) -> str:
    payload = json.dumps(
        {
            "type": self.type,
            "image": self.image,
            "repo": self.repo,
            "path": self.path,
            "env": self.env,
            "args": self.args,
            "build_secrets": self.build_secrets,
            "command": self.command,
            "project_env": self.project_env,
            "volumes": [list(v) for v in self.volumes],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class Application(BaseModel):
  project = ForeignKeyField(Project, backref="applications", on_delete="RESTRICT")
  name = AlphaNumericField()
  label = CharField()
  description = CharField(null=True)
  type = CharField(choices=["docker", "git"], default="docker")
  image = CharField(null=True)
  repo = CharField(null=True)
  path = CharField(default="Dockerfile")
  env = EncryptedTextField(null=True)
  args = EncryptedTextField(null=True)
  build_secrets = EncryptedTextField(null=True)
  command = CharField(null=True)
  status = CharField(choices=STATUS_CHOICES, default="inactive")
  container_count = IntegerField(default=0)
  deployed_at = DateTimeField(null=True)
  deployed_hash = CharField(null=True)

  @property
  def qualified_name(self) -> str:
    '''
      This is the name of the application used on the worker to prevent name space collisions.
    '''
    return f"{self.project.name}-{self.name}"

  @property
  def deploy_stamp(self) -> str | None:
    """Canonical string form of deployed_at."""
    if self.deployed_at is None:
      return None
    return self.deployed_at.strftime("%Y-%m-%dT%H:%M:%S.%f")

  @property
  def deploy_config(self) -> DeployConfig:
    """Point-in-time snapshot of this application's deploy inputs. Reads
    project.env and volumes lazily, so a caller needing a strictly consistent
    view (the deploy task) builds it inside a single transaction."""
    return DeployConfig(
        qualified_name=self.qualified_name,
        deploy_stamp=self.deploy_stamp,
        type=self.type,
        image=self.image,
        repo=self.repo,
        path=self.path,
        env=self.env or "",
        args=self.args or "",
        build_secrets=self.build_secrets or "",
        command=self.command or "",
        project_env=self.project.env or "",
        volumes=tuple(sorted((v.name, v.path) for v in self.volumes)),
    )

  @property
  def config_dirty(self) -> bool:
    """True when the current deploy inputs differ from those of the last deploy.
    A null baseline (never deployed) is not dirty."""
    return self.deployed_hash is not None and self.deploy_config.content_hash != self.deployed_hash

  def save(self, *args, **kwargs):
    if self.type == "docker":
      self.repo = None
      self.path = "Dockerfile"
      self.args = None
      self.build_secrets = None

    if self.type == "git":
      self.image = None
      if self.repo:
        pattern = r"https?://[\w.-]+/[\w.-]+/[\w.-]+\.git(?:#[\w.-]+)?(?::[\w./-]+)?"
        if not re.fullmatch(pattern, self.repo):
          raise ValueError(
              f"Invalid Git repository URL: {self.repo}. Should be in format 'https://github.com/user/repo.git<?#branch><?:sub-directory>'")

    if self.image and not re.fullmatch(r"[A-Za-z0-9][\w.\-/:@]*", self.image):
      raise ValueError(
          f"Invalid image reference: {self.image!r}. Allowed characters: letters, digits, and . _ - / : @")

    if self.env:
      validate_multiline_kv(self.env, "application.env")

    if self.args:
      validate_multiline_kv(self.args, "application.args")

    if self.build_secrets:
      validate_multiline_kv(self.build_secrets, "application.build_secrets")

    return super().save(*args, **kwargs)

  class Meta:
    indexes = ((("project", "name"), True),)


class Container(BaseModel):
  application = ForeignKeyField(Application, backref="containers", on_delete="RESTRICT")
  worker = ForeignKeyField(Worker, backref="containers")
  status = CharField(choices=STATUS_CHOICES, default="inactive")

  class Meta:
    indexes = ((("application", "worker"), True),)


RESERVED_DOMAIN_NAMES = {"core", "int", "discovery"}


class Domain(BaseModel):
  """A name linked to one container, which it routes to on `port`: public `<name>.<domain>`, or internal or TCP
  `<name>.<subdomain>.int.<domain>` under its worker's subdomain. Deleting the container unlinks it. `service` groups
  names for discovery, apart for each namespace (public, or internal and TCP), and `pool` is a subgroup of a service."""
  name = LabelField()
  type = CharField(choices=["internal", "public", "tcp"], default="internal")
  port = IntegerField()
  container = ForeignKeyField(Container, backref="domains", null=True, on_delete="SET NULL")
  # Internal and TCP: the worker whose subdomain the name is under, kept when the container is unlinked.
  worker = ForeignKeyField(Worker, backref="domains", null=True, on_delete="RESTRICT")
  service = LabelField(null=True)
  pool = LabelField(null=True)
  cors_origins = OriginListField(null=True)
  status = CharField(choices=["pending", "synced", "error"], default="pending")
  sync_error = CharField(null=True)
  synced_hash = CharField(null=True)
  synced_at = DateTimeField(null=True)

  def save(self, *args, **kwargs):
    if self.type == "tcp" and self.cors_origins is not None:
      self.cors_origins = None
    elif any(field is Domain.cors_origins for field in self.dirty_fields):
      self.cors_origins = Domain.cors_origins.validate(self.cors_origins)
    if self.type == "public":
      if self.name in RESERVED_DOMAIN_NAMES:
        raise ValueError(f"'{self.name}' is reserved for Sage's own addresses.")
      self.worker = None
    elif self.container_id is not None:
      self.worker = self.container.worker
    elif self.worker_id is None:
      raise ValueError("An internal or TCP domain without a container is not allowed.")

    namespace = Domain.worker == self.worker_id if self.worker_id else Domain.worker.is_null()
    if Domain.select().where((Domain.name == self.name) & namespace & (Domain.id != self.id)).exists():
      raise ValueError(f"'{self.name}' is already a public domain." if self.type == "public"
                       else f"'{self.name}' is already an internal or TCP domain on worker {self.worker_id}.")

    return super().save(*args, **kwargs)

  def host(self, base_domain: str) -> str | None:
    """The domain's full name; none for an internal or TCP one without a worker."""
    if self.type == "public":
      return f"{self.name}.{base_domain}"
    return f"{self.name}.{self.worker.subdomain}.int.{base_domain}" if self.worker_id else None

  def address(self, base_domain: str) -> str | None:
    """How a client reaches it: an https URL, or `host:8443` for TCP."""
    host = self.host(base_domain)
    if host is None:
      return None
    return f"{host}:8443" if self.type == "tcp" else f"https://{host}"

  def allowed_origins(self, base_domain: str) -> tuple[str, ...]:
    if self.type == "tcp" or self.host(base_domain) is None:
      return ()
    return tuple(sorted(self.cors_origins or [self.address(base_domain)]))


# A name once among the public domains, and once per worker among the internal and TCP ones.
Domain.add_index(Domain.index(Domain.name, SQL("coalesce(worker_id, '')"), unique=True, name="domain_name_worker"))


class Volume(BaseModel):
  name = AlphaNumericField()
  path = CharField()
  backup_cron = CharField(null=True)
  application = ForeignKeyField(Application, backref="volumes", on_delete="CASCADE")

  def save(self, *args, **kwargs):
    # Container-side mount target: must be absolute and free of the ':' separator
    # and quotes that would break the compose volume entry.
    if not re.fullmatch(r"/[^\n\r:\"]*", self.path or ""):
      raise ValueError(
          f"Invalid volume path: {self.path!r}. Must be an absolute path without ':' or quotes.")

    return super().save(*args, **kwargs)

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
  status = CharField(choices=["completed", "failed", "cancelled"])
  started_at = DateTimeField(null=True)  # none for a task cancelled before it started
  finished_at = DateTimeField(null=True)

  class Meta:
    indexes = ((("created_at",), False), (("finished_at",), False))
