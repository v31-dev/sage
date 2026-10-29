import inspect
from types import SimpleNamespace

from services.manager import Manager
from utils.queue import TaskQueue


class FakeS3:
  """The S3 calls the routes make; the signatures mirror the real ones."""

  def __init__(self):
    self.s3_config = {"bucket": "bucket"}
    self.keys = set()
    self.checked = []
    self.links = []

  def key_exists(self, s3_path, raw_path=False):
    self.checked.append({"s3_path": s3_path, "raw_path": raw_path})
    return s3_path in self.keys

  def create_presigned_download_url(self, s3_path, expires_in, raw_path=False, filename=None):
    self.links.append({"s3_path": s3_path, "expires_in": expires_in, "raw_path": raw_path, "filename": filename})
    return f"https://s3.example.test/{s3_path}"


class FakeTailscale:
  """The tailnet as the routes see it; the signatures mirror the real ones."""

  def __init__(self):
    self.machines = {}    # hostname -> ip
    self.paths = {}       # ip -> how the last ping arrived

  def path(self, target):
    return self.paths.get(target)

  def find_peer(self, query):
    query = query.strip().lower()
    for hostname, ip in self.machines.items():
      if query in (hostname.lower(), ip):
        return SimpleNamespace(hostname=hostname, ip=ip)
    return None


class FakeManager:
  """The part of Manager the routes use. A task method is only recorded by `add_task`, never run;
  every queue call is checked against the real signatures, and a method the real Manager lacks
  raises, so the fake cannot drift from it unnoticed."""

  def __init__(self):
    self.state = "running"
    self.tasks = []
    self.admits = True
    self.running = set()
    self.queued = set()
    self.s3 = FakeS3()
    self.worker_statuses = {}
    self.worker_list = []
    self.online = set()
    self.traefik_syncs = []

  def __getattr__(self, name):
    if not callable(getattr(Manager, name, None)):
      raise AttributeError(f"Manager has no method {name}")

    def task(*args, **kwargs):
      raise AssertionError(f"Manager.{name} was called; the fake only records tasks")
    task.__name__ = name
    return task

  def task_state(self):
    return self.state

  def add_task(self, **params):
    inspect.signature(TaskQueue.add_task).bind(None, **params)
    inspect.signature(getattr(Manager, params["task"].__name__)).bind(None, **(params.get("params") or {}))
    self.tasks.append(params)
    return self.admits

  def is_task_running(self, task, scopes=None):
    return task.__name__ in self.running

  def has_task(self, task, params=None):
    return task.__name__ in self.queued

  def backup_status(self):
    return Manager.backup_status(self)

  def task_snapshot(self):
    return {"running": [{"task_id": "t-1"}], "queued": []}

  def is_worker_online(self, hostname):
    return hostname in self.online

  def request_application_traefik_sync(self, application):
    self.traefik_syncs.append(application.id)

  def worker_entry(self, hostname):
    return Manager.worker_entry(self, hostname)

  def worker_status(self, hostname):
    return self.worker_statuses.get(hostname, "online" if self.is_worker_online(hostname) else "offline")

  def get_volume_backup_resource_error(self, application, volumes):
    return Manager.get_volume_backup_resource_error(self, application, volumes)
