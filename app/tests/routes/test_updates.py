import datetime
import ipaddress
import queue
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import socketio
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from engineio.exceptions import ConnectionError as EngineConnectionError
from fastapi.encoders import jsonable_encoder
from playhouse.shortcuts import model_to_dict
from socketio.exceptions import ConnectionError as SocketConnectionError

import config
import services.updates as updates_module
from api import build_asgi_app
from routes.applications import load_application
from routes.backups import get_platform_backup_status
from routes.info import get as info
from routes.info import get_release
from routes.notifications import list_notifications
from routes.tasks import list_tasks
from routes.updates import TOPICS, same_origin, sio
from routes.workers import get as list_workers
from routes.workers import load_worker
from services.db import Notification
from services.updates import Feed, Topic, UpdateFeed, Updates, UpdateSource, UpdateTopic

BOTH = ["polling", "websocket"]
SAME_ORIGIN = object()
NO_ORIGIN = None
REFUSED = (SocketConnectionError, EngineConnectionError)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
  monkeypatch.setattr(config, "UPDATES_COALESCE_SECONDS", 0.02)


@pytest.fixture(autouse=True)
def own_pool(monkeypatch):
  """A pool of its own, so its threads do not keep a database connection from one test to the next."""
  pool = ThreadPoolExecutor(max_workers=2)
  monkeypatch.setattr(updates_module, "UPDATES_EXECUTOR", pool)
  yield
  pool.shutdown()


def serve(app, monkeypatch, **options):
  """The app on a free port, in a thread; returns the port and a function that stops it."""
  monkeypatch.setattr(sio.eio, "ping_interval", 1)
  monkeypatch.setattr(sio.eio, "ping_timeout", 1)
  listener = socket.socket()
  listener.bind(("127.0.0.1", 0))
  port = listener.getsockname()[1]
  server = uvicorn.Server(uvicorn.Config(app, log_config=None, loop="asyncio", timeout_graceful_shutdown=1, **options))
  thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
  thread.start()
  deadline = time.monotonic() + 10
  while not server.started:
    assert time.monotonic() < deadline, "the server did not start"
    time.sleep(0.01)

  def stop():
    server.should_exit = True
    thread.join(10)

  return port, stop


@pytest.fixture
def served(manager, thread_connections, monkeypatch):
  """The app as the servers serve it, on a free port: the Socket.IO server in front of the API."""
  port, stop = serve(build_asgi_app(False), monkeypatch)
  yield port
  stop()


@pytest.fixture
def served_over_tls(manager, thread_connections, monkeypatch, tmp_path):
  """The same, with TLS of its own as in production, where no proxy is in front."""
  key = ec.generate_private_key(ec.SECP256R1())
  name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
  now = datetime.datetime.now(datetime.timezone.utc)
  certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                 .not_valid_after(now + datetime.timedelta(days=1))
                 .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                 .sign(key, hashes.SHA256()))
  (tmp_path / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
  (tmp_path / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
  port, stop = serve(build_asgi_app(False), monkeypatch, ssl_certfile=str(tmp_path / "cert.pem"),
                     ssl_keyfile=str(tmp_path / "key.pem"))
  yield port
  stop()


class Browser:
  """A Socket.IO client that keeps what it is sent."""

  def __init__(self, port, transports=None, origin=SAME_ORIGIN, headers=None, tls=False):
    """`origin` is what the page sends: its own origin, another one, or none."""
    self.port = port
    scheme = "https" if tls else "http"
    self.received = queue.Queue()
    headers = dict(headers or {})
    if origin is not NO_ORIGIN:
      headers["Origin"] = f"{scheme}://127.0.0.1:{port}" if origin is SAME_ORIGIN else origin
    self.client = socketio.Client(reconnection=False, ssl_verify=False, websocket_extra_options={"suppress_origin": True})
    self.client.on("state", lambda payload: self.received.put(("state", payload)))
    self.client.on("topic_error", lambda payload: self.received.put(("topic_error", payload)))
    self.client.connect(f"{scheme}://127.0.0.1:{port}", socketio_path="api/socket.io", transports=transports or BOTH,
                        headers=headers, wait_timeout=5)

  def subscribe(self, topic):
    """Subscribe, and wait until the manager says it has."""
    self.client.call("subscribe", topic, timeout=5)

  def unsubscribe(self, topic):
    self.client.call("unsubscribe", topic, timeout=5)

  def next(self):
    return self.received.get(timeout=5)

  def close(self):
    self.client.disconnect()


@pytest.fixture
def browser(served):
  opened = []

  def open_one(transports=None, **options):
    opened.append(Browser(served, transports, **options))
    return opened[-1]

  yield open_one
  for one in opened:
    if one.client.connected:
      one.close()


@pytest.fixture
def state(monkeypatch):
  state = {"value": 1}
  monkeypatch.setitem(TOPICS, "info", Topic(lambda: dict(state), frozenset({UpdateSource.QUEUE_STATE})))
  return state


class TestTopics:
  def test_the_topics_are_the_ones_the_pages_follow(self):
    assert set(TOPICS) == {"info", "tasks", "backup-status", "release", "workers", "worker", "application", "notifications",
                           "task-history"}

  def test_the_table_has_each_topic_and_each_feed_once(self):
    assert not set(UpdateTopic) & set(UpdateFeed)
    assert set(TOPICS) == set(UpdateTopic) | set(UpdateFeed)
    assert len(TOPICS) == len(UpdateTopic) + len(UpdateFeed)

  def test_the_state_topics_are_topics_and_the_feeds_are_feeds(self):
    assert all(isinstance(TOPICS[name], Topic) for name in UpdateTopic)
    assert all(isinstance(TOPICS[name], Feed) for name in UpdateFeed)

  def test_the_info_follows_what_the_manager_status_is_made_of(self):
    assert TOPICS["info"].sources == {UpdateSource.QUEUE_STATE, UpdateSource.RESTORING, UpdateSource.SETTINGS_APPLYING,
                                      UpdateSource.CHECKS, UpdateSource.LATEST_VERSION}

  @pytest.mark.parametrize("topic, source", [("tasks", UpdateSource.TASKS), ("backup-status", UpdateSource.BACKUP_STATUS),
                                             ("release", UpdateSource.RELEASE), ("workers", UpdateSource.WORKER_LIST)])
  def test_the_other_topics_follow_the_one_source_they_show(self, topic, source):
    assert TOPICS[topic].sources == {source}

  def test_a_worker_and_an_application_are_parameterised_and_follow_the_sources_of_their_own_resource(self):
    assert TOPICS["worker"].parameterised and TOPICS["application"].parameterised
    assert TOPICS["worker"].sources == {UpdateSource.WORKER_ENTRY, UpdateSource.WORKER_CONTAINERS}
    assert TOPICS["application"].sources == {UpdateSource.APPLICATION}
    assert not any(TOPICS[name].parameterised for name in ("info", "tasks", "backup-status", "release", "workers"))

  @pytest.mark.parametrize("topic, route", [("info", info), ("tasks", list_tasks), ("backup-status", get_platform_backup_status),
                                            ("release", get_release), ("workers", list_workers), ("worker", load_worker),
                                            ("application", load_application)])
  def test_a_topic_is_loaded_by_the_function_of_its_route(self, topic, route):
    assert TOPICS[topic].load is route

  def test_the_server_pings_at_the_configured_interval(self):
    assert sio.eio.ping_interval == config.UPDATES_PING_INTERVAL_SECONDS
    assert sio.eio.ping_timeout == config.UPDATES_PING_TIMEOUT_SECONDS


@pytest.mark.parametrize("transports", [["polling"], ["websocket"], BOTH], ids=["long-polling", "websocket", "upgrading"])
class TestOverEachTransport:
  def test_a_subscriber_is_sent_nothing_when_it_subscribes_and_then_each_change(self, browser, state, transports):
    one = browser(transports)
    one.subscribe("info")
    assert one.received.empty()
    state["value"] = 2
    Updates().changed(UpdateSource.QUEUE_STATE)
    assert one.next() == ("state", {"topic": "info", "data": {"value": 2}})
    state["value"] = 3
    Updates().changed(UpdateSource.QUEUE_STATE)
    assert one.next() == ("state", {"topic": "info", "data": {"value": 3}})

  def test_an_unknown_topic_is_an_error(self, browser, transports):
    one = browser(transports)
    one.subscribe("nothing")
    assert one.next() == ("topic_error", {"topic": "nothing", "status": 404})

  @pytest.mark.parametrize("topic", [5, None, ["info"], {"topic": "info"}])
  def test_a_message_that_is_not_a_topic_name_is_ignored_and_the_connection_stays(self, browser, state, transports, topic):
    one = browser(transports)
    one.client.call("subscribe", topic, timeout=5)
    one.client.call("unsubscribe", topic, timeout=5)
    one.subscribe("info")
    Updates().changed(UpdateSource.QUEUE_STATE)
    assert one.next() == ("state", {"topic": "info", "data": {"value": 1}})
    assert one.received.empty()

  def test_after_unsubscribing_nothing_more_is_pushed_for_the_topic(self, browser, state, transports):
    one = browser(transports)
    one.subscribe("info")
    one.unsubscribe("info")
    state["value"] = 2
    Updates().changed(UpdateSource.QUEUE_STATE)
    one.subscribe("notifications")
    Updates().publish(UpdateFeed.NOTIFICATIONS, [{"id": 1}])
    assert one.next() == ("state", {"topic": "notifications", "data": [{"id": 1}]})

  def test_two_browsers_each_get_the_change(self, browser, state, transports):
    first, second = browser(transports), browser(transports)
    for one in (first, second):
      one.subscribe("info")
    state["value"] = 3
    Updates().changed(UpdateSource.QUEUE_STATE)
    assert first.next()[1]["data"] == {"value": 3}
    assert second.next()[1]["data"] == {"value": 3}

  def test_a_new_notification_is_pushed_alone_in_the_shape_the_list_returns(self, browser, db, transports):
    one = browser(transports)
    one.subscribe("notifications")
    assert one.received.empty()
    notification = Notification.create(content="deployed", type="success", link="https://example.test")
    Updates().publish(UpdateFeed.NOTIFICATIONS, [model_to_dict(notification, recurse=False)])
    event, payload = one.next()
    assert event == "state" and payload["topic"] == "notifications"
    assert [row["content"] for row in payload["data"]] == ["deployed"]
    assert payload["data"] == jsonable_encoder(list_notifications())


class TestHandshake:
  @pytest.mark.parametrize("transports", [["polling"], ["websocket"]], ids=["long-polling", "websocket"])
  @pytest.mark.parametrize("origin", ["http://evil.example", "https://127.0.0.1", "null"])
  def test_a_page_of_another_origin_is_refused(self, served, transports, origin):
    with pytest.raises(REFUSED):
      Browser(served, transports, origin)

  @pytest.mark.parametrize("transports", [["polling"], ["websocket"]], ids=["long-polling", "websocket"])
  def test_a_page_of_the_same_origin_is_accepted(self, served, transports):
    one = Browser(served, transports, SAME_ORIGIN)
    assert one.client.connected
    one.close()

  @pytest.mark.parametrize("transports", [["polling"], ["websocket"]], ids=["long-polling", "websocket"])
  def test_a_client_that_sends_no_origin_is_accepted(self, served, transports):
    one = Browser(served, transports, NO_ORIGIN)
    assert one.client.connected
    one.close()

  @pytest.mark.parametrize("transports, forwarded", [(["polling"], "https"), (["websocket"], "wss")],
                           ids=["long-polling", "websocket"])
  def test_a_page_behind_a_proxy_is_accepted_when_the_proxy_says_how_it_was_reached(self, served, transports, forwarded):
    """Traefik says `https` for an ordinary request and `wss` for a WebSocket handshake."""
    one = Browser(served, transports, f"https://127.0.0.1:{served}", {"X-Forwarded-Proto": forwarded})
    assert one.client.connected
    one.close()

  def test_the_upgrade_behind_a_proxy_reaches_a_websocket(self, served, state):
    one = Browser(served, BOTH, f"https://127.0.0.1:{served}", {"X-Forwarded-Proto": "https"})
    deadline = time.monotonic() + 5
    while one.client.transport() != "websocket" and time.monotonic() < deadline:
      time.sleep(0.05)
    assert one.client.transport() == "websocket"
    one.close()

  @pytest.mark.parametrize("transports", [["polling"], ["websocket"]], ids=["long-polling", "websocket"])
  def test_a_page_of_a_secure_origin_is_refused_when_nothing_says_it_was_reached_securely(self, served, transports):
    with pytest.raises(REFUSED):
      Browser(served, transports, f"https://127.0.0.1:{served}")


class TestHandshakeWithTls:
  """In production the manager serves TLS itself and no proxy is in front of it."""

  @pytest.mark.parametrize("transports", [["polling"], ["websocket"], BOTH], ids=["long-polling", "websocket", "upgrading"])
  def test_a_page_of_the_same_secure_origin_is_accepted(self, served_over_tls, transports):
    one = Browser(served_over_tls, transports, SAME_ORIGIN, tls=True)
    assert one.client.connected
    one.close()

  def test_the_upgrade_reaches_a_websocket(self, served_over_tls):
    one = Browser(served_over_tls, BOTH, SAME_ORIGIN, tls=True)
    deadline = time.monotonic() + 5
    while one.client.transport() != "websocket" and time.monotonic() < deadline:
      time.sleep(0.05)
    assert one.client.transport() == "websocket"
    one.close()

  @pytest.mark.parametrize("transports", [["polling"], ["websocket"]], ids=["long-polling", "websocket"])
  @pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1", "null"])
  def test_a_page_of_another_origin_is_refused(self, served_over_tls, transports, origin):
    with pytest.raises(REFUSED):
      Browser(served_over_tls, transports, origin, tls=True)

  @pytest.mark.parametrize("transports", [["polling"], ["websocket"]], ids=["long-polling", "websocket"])
  def test_the_same_host_over_plain_http_is_refused(self, served_over_tls, transports):
    with pytest.raises(REFUSED):
      Browser(served_over_tls, transports, f"http://127.0.0.1:{served_over_tls}", tls=True)


class TestSameOrigin:
  def environ(self, scheme, host="manager.example"):
    return {"asgi.scope": {"scheme": scheme}, "HTTP_HOST": host}

  @pytest.mark.parametrize("scheme, origin", [("http", "http://manager.example"), ("https", "https://manager.example"),
                                              ("ws", "http://manager.example"), ("wss", "https://manager.example")])
  def test_the_page_of_the_scheme_and_host_the_request_came_with_is_the_same_origin(self, scheme, origin):
    assert same_origin(origin, self.environ(scheme)) is True

  @pytest.mark.parametrize("scheme, origin", [("https", "http://manager.example"), ("wss", "http://manager.example"),
                                              ("http", "https://manager.example"), ("ws", "https://manager.example"),
                                              ("https", "https://other.example"), ("https", "https://manager.example:8443"),
                                              ("https", "null"), ("https", "")])
  def test_any_other_origin_is_not(self, scheme, origin):
    assert same_origin(origin, self.environ(scheme)) is False

  def test_the_port_of_the_host_counts(self):
    assert same_origin("https://manager.example:8443", self.environ("https", "manager.example:8443")) is True
    assert same_origin("https://manager.example", self.environ("https", "manager.example:8443")) is False

  def test_a_request_without_a_host_has_no_same_origin(self):
    assert same_origin("https://manager.example", {"asgi.scope": {"scheme": "https"}}) is False

  def test_a_scheme_that_is_not_known_has_no_same_origin(self):
    assert same_origin("ftp://manager.example", self.environ("ftp")) is False


class TestWhilePaused:
  def test_the_connection_is_served_while_the_manager_refuses_changes(self, browser, state, manager):
    manager.state = "paused"
    one = browser(["polling"])
    one.subscribe("info")
    Updates().changed(UpdateSource.QUEUE_STATE)
    assert one.next()[1]["data"] == {"value": 1}
    one.close()


class TestOneWorkerAndOneApplication:
  def test_a_worker_is_followed_by_its_hostname_and_pushed_what_its_route_returns(self, browser, client, worker, manager):
    manager.worker_list = [{"hostname": "w1", "ip": "100.64.0.2", "online": True, "status": "online", "path": None,
                            "container_count": None, "infra": None}]
    one = browser()
    one.subscribe("worker:w1")
    assert one.received.empty()
    manager.worker_list = [{**manager.worker_list[0], "status": "rebooting"}]
    Updates().changed(UpdateSource.WORKER_ENTRY, "w1")
    event, message = one.next()
    assert (event, message["topic"]) == ("state", "worker:w1")
    assert message["data"] == jsonable_encoder(client.get("/api/workers/w1").json())
    assert message["data"]["status"] == "rebooting"

  def test_an_application_is_followed_by_its_project_and_name_and_pushed_what_its_route_returns(self, browser, client, application):
    one = browser()
    one.subscribe("application:demo/web")
    application.status = "deploying"
    application.save()
    Updates().changed(UpdateSource.APPLICATION, "demo/web")
    event, message = one.next()
    assert (event, message["topic"]) == ("state", "application:demo/web")
    assert message["data"] == client.get("/api/projects/demo/applications/web").json()
    assert message["data"]["status"] == "deploying"

  def test_another_application_is_not_pushed_to_this_ones_followers(self, browser, application):
    from services.db import Application
    Application.create(project=application.project, name="api", label="Api", image="nginx")
    one = browser()
    one.subscribe("application:demo/web")
    Updates().changed(UpdateSource.APPLICATION, "demo/api")
    Updates().changed(UpdateSource.APPLICATION, "demo/web")
    event, message = one.next()
    assert message["topic"] == "application:demo/web" and message["data"]["name"] == "web"
    assert one.received.empty()

  def test_another_worker_is_not_pushed_to_this_ones_followers(self, browser, worker, manager):
    manager.worker_list = [{"hostname": "w1", "ip": "100.64.0.2", "online": True, "status": "online", "path": None,
                            "container_count": None, "infra": None}]
    one = browser()
    one.subscribe("worker:w1")
    Updates().changed(UpdateSource.WORKER_CONTAINERS, "w2")
    Updates().changed(UpdateSource.WORKER_CONTAINERS, "w1")
    assert one.next()[1]["topic"] == "worker:w1"
    assert one.received.empty()

  def test_two_projects_may_each_have_an_application_of_the_same_name(self, browser, application):
    from services.db import Application, Project
    other = Project.create(name="other", label="Other")
    Application.create(project=other, name="web", label="Elsewhere", image="nginx")
    here, there = browser(), browser()
    here.subscribe("application:demo/web")
    there.subscribe("application:other/web")
    Updates().changed(UpdateSource.APPLICATION, "demo/web")
    Updates().changed(UpdateSource.APPLICATION, "other/web")
    assert (here.next()[1]["data"]["label"], there.next()[1]["data"]["label"]) == ("Web", "Elsewhere")

  @pytest.mark.parametrize("name", ["worker", "worker:", "application", "application:", "info:demo", "tasks:1"])
  def test_a_name_without_the_argument_it_needs_or_with_one_it_does_not_take_is_refused(self, browser, name):
    one = browser()
    one.subscribe(name)
    assert one.next() == ("topic_error", {"topic": name, "status": 404})


class TestLeaving:
  def test_a_browser_that_disconnects_is_forgotten(self, browser, state):
    one = browser()
    one.subscribe("info")
    assert Updates()._subscribers["info"]
    one.close()
    deadline = time.monotonic() + 5
    while "info" in Updates()._subscribers and time.monotonic() < deadline:
      time.sleep(0.01)
    assert "info" not in Updates()._subscribers
