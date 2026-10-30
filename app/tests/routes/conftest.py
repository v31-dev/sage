import pytest
from fastapi.testclient import TestClient
from peewee import SqliteDatabase

from services.manager import Manager
from services.tailscale import Tailscale
from tests.routes.fakes import FakeManager, FakeTailscale


@pytest.fixture
def manager(register):
  return register(Manager, FakeManager())


@pytest.fixture
def tailscale(register):
  return register(Tailscale, FakeTailscale())


@pytest.fixture
def s3(manager):
  return manager.s3


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
def client(manager, thread_connections):
  from api import app
  return TestClient(app)


@pytest.fixture
def volume(application):
  from services.db import Volume
  return Volume.create(application=application, name="data", path="/data")
