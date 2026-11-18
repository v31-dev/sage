import pytest
from fastapi.testclient import TestClient

from services.manager import Manager
from tests.routes.fakes import ManagerStandIn


@pytest.fixture
def manager(register):
  return register(Manager, ManagerStandIn())


@pytest.fixture
def client(manager, thread_connections):
  from api import app
  return TestClient(app)
