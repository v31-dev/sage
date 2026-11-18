from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.manager import Manager
from services.tailscale import Tailscale
from tests.routes.fakes import MANAGER_IP, ManagerStandIn


@pytest.fixture
def manager(register):
  return register(Manager, ManagerStandIn())


@pytest.fixture
def client(manager, thread_connections):
  from api import app
  return TestClient(app)


@pytest.fixture
def tailscale(register):
  return register(Tailscale, SimpleNamespace(ip=lambda: MANAGER_IP))
