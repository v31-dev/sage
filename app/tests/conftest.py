import os

import pytest
from cryptography.fernet import Fernet

# Set before any test module imports the app: utils.db reads the key at import.
os.environ.update(
    ENCRYPTION_KEY=Fernet.generate_key().decode(),
    DOMAIN="example.test",
    CLOUDFLARE_ACCOUNT_ID="test-account",
    CLOUDFLARE_API_TOKEN="test-token",
    TS_NAME="manager",
    TS_WORKER_TAG="tag:worker",
    ENV="development",
)


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
