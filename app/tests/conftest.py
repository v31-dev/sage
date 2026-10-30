import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from peewee import SqliteDatabase

# Set before any test module imports the app: utils.db reads the key at import.
# TZ puts the suite away from UTC (+5:30), so a time read from the local clock fails a test.
os.environ.update(
    TZ="Asia/Kolkata",
    ENCRYPTION_KEY=Fernet.generate_key().decode(),
    DOMAIN="example.test",
    CLOUDFLARE_API_TOKEN="test-token",
    TS_NAME="manager",
    ENV="development",
)
time.tzset()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
  """A known bug keeps the run red until it is fixed: an expected failure, whether set by a
  marker or by pytest.xfail(), is reported as a failure. pytest leaves a failure out of the
  exit code while the report has `wasxfail`, so that is removed."""
  report = yield
  if hasattr(report, "wasxfail"):
    reason = report.wasxfail
    del report.wasxfail
    report.outcome = "failed"
    report.longrepr = f"{item.nodeid}: xfail is not allowed, fix the bug instead ({reason or 'no reason given'})."
  return report


@pytest.fixture(autouse=True)
def _reset_singletons():
  from services.base import Singleton
  Singleton._instances.clear()
  Singleton._locks.clear()
  yield
  Singleton._instances.clear()
  Singleton._locks.clear()


class Clock:
  """The frozen utcnow(): `clock.set(datetime(2026, 1, 1, 12))`, `clock.advance(timedelta(minutes=1))`."""

  def __init__(self):
    self.current = datetime(2026, 1, 1, 12, 0, 0)

  def set(self, value: datetime):
    self.current = value

  def advance(self, delta: timedelta):
    self.current += delta


@pytest.fixture
def clock(monkeypatch):
  """Freezes `utils.common.utcnow()` at 2026-01-01 12:00 UTC until the test moves it."""
  from utils import common
  frozen = Clock()

  class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
      assert tz is timezone.utc, "utcnow() reads the clock in UTC"
      return frozen.current.replace(tzinfo=timezone.utc)

  monkeypatch.setattr(common, "datetime", FrozenDatetime)
  return frozen


@pytest.fixture
def machine_timezone(monkeypatch):
  """Runs the test in another time zone: `machine_timezone("Asia/Kolkata")`."""

  def use(name: str):
    monkeypatch.setenv("TZ", name)
    time.tzset()

  yield use
  monkeypatch.undo()
  time.tzset()


@pytest.fixture
def register():
  """Put a fake where a service singleton would be built: `register(Cloudflare, fake)`."""
  from services.base import Singleton

  def _register(cls, fake):
    Singleton._instances[cls] = fake
    return fake

  return _register


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
  import config
  monkeypatch.setattr(config, "DATA_DIR", tmp_path)
  monkeypatch.setattr(config, "DB_PATH", tmp_path / "data.db")
  return tmp_path


@pytest.fixture
def db(data_dir):
  from services.db import Database
  from services.db import db as database
  Database()
  yield database
  database.close()


@pytest.fixture
def thread_connections(db, monkeypatch):
  """Closes the connections the app's worker threads open, to the database and to the log files. Each test client
  starts its own short-lived threads, and Python 3.13 and later warn about a connection left to garbage collection."""
  opened = []
  connect = SqliteDatabase._connect

  def tracked(database):
    database.connect_params["check_same_thread"] = False
    connection = connect(database)
    opened.append(connection)
    return connection

  monkeypatch.setattr(SqliteDatabase, "_connect", tracked)
  yield
  for connection in opened:
    connection.close()


@pytest.fixture
def project(db):
  from services.db import Project
  return Project.create(name="demo", label="Demo")


@pytest.fixture
def application(project):
  from services.db import Application
  return Application.create(project=project, name="web", label="Web", image="nginx")


@pytest.fixture
def worker(db):
  from services.db import Worker
  return Worker.create(hostname="w1", ip="100.64.0.2")
