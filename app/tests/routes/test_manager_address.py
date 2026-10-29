from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from api import OnlyManagerAddress, build_asgi_app
from services.cloudflare import Cloudflare

MANAGER = "manager.core.example.test"


async def page(request):
  return PlainTextResponse("page")


async def socket(websocket):
  await websocket.accept()
  await websocket.send_text("open")
  await websocket.close()


class AtHost(TestClient):
  """A client of the app at a host: its WebSocket requests, which the test client sends to `testserver`, too."""

  def __init__(self, app, host):
    super().__init__(app, base_url=f"http://{host}", follow_redirects=False)
    self.host = host

  def websocket_connect(self, url, *args, **kwargs):
    return super().websocket_connect(f"ws://{self.host}{url}", *args, **kwargs)


@pytest.fixture
def at(register):
  fake = SimpleNamespace(manager_hostname=MANAGER)
  fake.is_manager_hostname = lambda hostname: Cloudflare.is_manager_hostname(fake, hostname)
  register(Cloudflare, fake)
  app = Starlette(routes=[Route("/page", page), WebSocketRoute("/ws", socket)])
  app.add_middleware(OnlyManagerAddress)

  def client(host):
    return AtHost(app, host)

  return client


class TestHttp:
  def test_the_manager_address_is_answered(self, at):
    assert at(MANAGER).get("/page").text == "page"

  def test_the_address_is_matched_in_any_case(self, at):
    assert at(MANAGER.upper()).get("/page").text == "page"

  @pytest.mark.parametrize("host", ["100.64.0.1", "manager", "other.example.test"])
  def test_another_name_is_sent_to_the_manager_address(self, at, host):
    response = at(host).get("/page?x=1")
    assert response.status_code == 308
    assert response.headers["location"] == f"https://{MANAGER}/page?x=1"


class TestWebSocket:
  def test_the_manager_address_is_served(self, at):
    with at(MANAGER).websocket_connect("/ws") as connection:
      assert connection.receive_text() == "open"

  @pytest.mark.parametrize("host", ["100.64.0.1", "manager", "other.example.test"])
  def test_another_name_is_refused(self, at, host):
    with pytest.raises(WebSocketDisconnect) as refused:
      with at(host).websocket_connect("/ws"):
        pass
    assert refused.value.code == 1008


class TestLifespan:
  def test_the_application_starts_and_stops_through_it(self, at):
    with at(MANAGER) as client:
      assert client.get("/page").status_code == 200


class TestTheComposedApp:
  """The Socket.IO server in front of the API, as the servers serve it."""

  URL = "/api/socket.io/?EIO=4&transport=polling"

  @pytest.fixture
  def composed(self, register):
    fake = SimpleNamespace(manager_hostname=MANAGER)
    fake.is_manager_hostname = lambda hostname: Cloudflare.is_manager_hostname(fake, hostname)
    register(Cloudflare, fake)

    def client(production, host):
      return TestClient(build_asgi_app(production), base_url=f"http://{host}", follow_redirects=False)

    return client

  def test_in_production_the_manager_address_is_served(self, composed):
    response = composed(True, MANAGER).get(self.URL)
    assert response.status_code == 200
    assert '"sid"' in response.text

  @pytest.mark.parametrize("host", ["100.64.0.1", "manager"])
  def test_in_production_another_name_is_sent_to_the_manager_address(self, composed, host):
    response = composed(True, host).get(self.URL)
    assert response.status_code == 308
    assert response.headers["location"] == f"https://{MANAGER}{self.URL}"

  def test_in_development_any_name_is_served(self, composed):
    assert composed(False, "100.64.0.1").get(self.URL).status_code == 200
