import pytest
from fastapi.testclient import TestClient

from services.manager import Manager
from tests.routes.fakes import FakeManager


@pytest.fixture
def manager(register):
  return register(Manager, FakeManager())


@pytest.fixture
def s3(manager):
  return manager.s3


@pytest.fixture
def client(manager, db):
  from api import app
  return TestClient(app)


@pytest.fixture
def volume(application):
  from services.db import Volume
  return Volume.create(application=application, name="data", path="/data")
