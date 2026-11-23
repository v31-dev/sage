import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from utils.api import RefuseOtherSites

OWN = "https://manager.core.example.test"
LINK = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


async def page(request):
  return PlainTextResponse("page")


async def socket(websocket):
  await websocket.accept()
  await websocket.send_text("open")
  await websocket.close()


def client(origin):
  app = Starlette(routes=[Route("/page", page, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]),
                          WebSocketRoute("/ws", socket)])
  return TestClient(RefuseOtherSites(app, origin))


@pytest.fixture
def manager():
  return client(lambda: OWN)


@pytest.fixture
def ingest():
  return client(None)


class TestFetchMetadata:
  @pytest.mark.parametrize("site", ["same-origin", "none"])
  @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
  def test_the_managers_own_page_and_the_user_pass(self, manager, site, method):
    assert manager.request(method, "/page", headers={"Sec-Fetch-Site": site}).status_code == 200

  @pytest.mark.parametrize("site", ["cross-site", "same-site"])
  @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
  def test_another_sites_request_is_refused_by_any_method(self, manager, site, method):
    response = manager.request(method, "/page", headers={"Sec-Fetch-Site": site, "Origin": OWN})
    assert response.status_code == 403
    assert response.json() == {"detail": "Requests from other sites are refused."}

  @pytest.mark.parametrize("site", ["cross-site", "same-site"])
  def test_a_link_followed_from_another_site_opens_the_page(self, manager, site):
    assert manager.get("/page", headers={"Sec-Fetch-Site": site, **LINK}).status_code == 200

  @pytest.mark.parametrize("headers", [
      {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"},
      {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
      {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
      {"Sec-Fetch-Mode": "navigate"},
  ], ids=["frame", "image", "fetch", "no-destination"])
  def test_anything_else_from_another_site_is_refused(self, manager, headers):
    assert manager.get("/page", headers={"Sec-Fetch-Site": "cross-site", **headers}).status_code == 403

  def test_a_form_another_site_submits_is_refused(self, manager):
    response = manager.post("/page", headers={"Sec-Fetch-Site": "cross-site", **LINK,
                                              "Content-Type": "application/x-www-form-urlencoded"}, content="x=1")
    assert response.status_code == 403

  def test_the_header_decides_over_the_origin(self, manager):
    assert manager.post("/page", headers={"Sec-Fetch-Site": "cross-site", "Origin": OWN}).status_code == 403
    assert manager.post("/page", headers={"Sec-Fetch-Site": "same-origin", "Origin": "null"}).status_code == 200


class TestOrigin:
  """A browser that sends no Fetch Metadata."""

  @pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
  def test_the_managers_own_origin_passes(self, manager, method):
    assert manager.request(method, "/page", headers={"Origin": OWN}).status_code == 200

  @pytest.mark.parametrize("origin", [
      "https://evil.example", "http://manager.core.example.test", "https://manager.core.example.test:8443",
      "https://app.example.test", "null",
  ], ids=["another-site", "plain-http", "another-port", "another-name", "opaque"])
  @pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
  def test_any_other_origin_is_refused(self, manager, origin, method):
    assert manager.request(method, "/page", headers={"Origin": origin}).status_code == 403

  def test_the_origin_is_read_at_each_request(self):
    current = {"origin": OWN}
    one = client(lambda: current["origin"])
    assert one.post("/page", headers={"Origin": OWN}).status_code == 200
    current["origin"] = "https://renamed.core.example.test"
    assert one.post("/page", headers={"Origin": OWN}).status_code == 403


class TestNotABrowser:
  @pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
  def test_a_request_with_neither_header_passes(self, manager, method):
    assert manager.request(method, "/page").status_code == 200


class TestWithoutAnOrigin:
  """The ingest server, which no browser calls."""

  def test_a_request_with_neither_header_passes(self, ingest):
    assert ingest.post("/page").status_code == 200

  @pytest.mark.parametrize("headers", [
      {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {"Sec-Fetch-Site": "cross-site", **LINK},
      {"Origin": OWN}, {"Origin": "null"},
  ], ids=["same-origin", "typed", "followed-link", "an-origin", "opaque-origin"])
  def test_every_browser_request_is_refused(self, ingest, headers):
    assert ingest.post("/page", headers=headers).status_code == 403
    assert ingest.get("/page", headers=headers).status_code == 403


class TestWebSocket:
  @pytest.mark.parametrize("headers", [{}, {"Origin": OWN}, {"Origin": OWN, "Sec-Fetch-Site": "same-origin"}],
                           ids=["no-browser", "own-origin", "same-origin"])
  def test_the_managers_own_page_connects(self, manager, headers):
    with manager.websocket_connect("/ws", headers=headers) as connection:
      assert connection.receive_text() == "open"

  @pytest.mark.parametrize("headers", [
      {"Origin": "https://evil.example"},
      {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "websocket"},
      {"Origin": OWN, "Sec-Fetch-Site": "cross-site", **LINK},
  ], ids=["another-origin", "cross-site", "shaped-like-a-link"])
  def test_another_sites_socket_is_closed(self, manager, headers):
    with pytest.raises(WebSocketDisconnect) as refused:
      with manager.websocket_connect("/ws", headers=headers):
        pass
    assert refused.value.code == 1008
