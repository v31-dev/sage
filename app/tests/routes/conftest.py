import pytest
from fastapi.testclient import TestClient

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
def client(manager, thread_connections):
  from api import app
  return TestClient(app)


@pytest.fixture
def volume(application):
  from services.db import Volume
  return Volume.create(application=application, name="data", path="/data")
