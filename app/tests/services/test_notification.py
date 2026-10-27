import logging
from types import SimpleNamespace

import pytest
import requests

from services import notification
from services.notification import (CHANNELS, NOTIFICATION_STYLES, TEST_NOTIFICATION, ChannelCheckFailed, Notifications, _request_webhook,
                                   webhook_failure)
from services.settings import Settings

URL = "https://discord.test/api/webhooks/1/SECRET-TOKEN"


def http_response(status):
  response = requests.Response()
  response.status_code = status
  response.url = URL
  return response


def discord_payload(instance, source="manager example.test"):
  return CHANNELS["discord_webhook"]["payload"](instance, source)


class FakeSession:
  def __init__(self):
    self.calls = []
    self.outcome = http_response(204)

  def request(self, method, url, **kwargs):
    self.calls.append((method, url, kwargs))
    if isinstance(self.outcome, Exception):
      raise self.outcome
    return self.outcome


@pytest.fixture
def session(monkeypatch):
  fake = FakeSession()
  monkeypatch.setattr(notification, "_session", fake)
  return fake


@pytest.fixture
def webhook(monkeypatch):
  """Replaces the webhook request: `failure` is what it returns (or raises, if an exception)."""
  state = SimpleNamespace(failure=None, calls=[])

  def fake(method, url, name, **kwargs):
    state.calls.append((method, url, name, kwargs))
    if isinstance(state.failure, Exception):
      raise state.failure
    return state.failure

  monkeypatch.setattr(notification, "_request_webhook", fake)
  return state


@pytest.fixture
def notifications(db):
  return Notifications()


@pytest.fixture
def configured_notifications(db):
  Settings().set("notifications", {"discord_webhook": URL})
  return Notifications()


class TestWebhookFailure:
  @pytest.mark.parametrize("status, transient", [(400, False), (401, False), (404, False), (429, True), (500, True), (503, True)])
  def test_an_http_error_names_the_status_and_says_whether_it_clears(self, status, transient):
    error = requests.HTTPError(response=http_response(status))
    assert webhook_failure(error, "Discord") == (f"Discord answered HTTP {status}", transient)

  @pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectTimeout()])
  def test_a_timeout_clears_by_itself(self, error):
    assert webhook_failure(error, "Discord") == ("Discord did not answer in time", True)

  def test_a_connection_error_clears_by_itself(self):
    assert webhook_failure(requests.ConnectionError(), "Discord") == ("could not reach Discord", True)

  def test_anything_else_is_not_transient_and_names_only_its_type(self):
    assert webhook_failure(ValueError("boom"), "Discord") == ("Discord request failed (ValueError)", False)

  @pytest.mark.parametrize("error", [
      requests.HTTPError(f"500 Server Error for url: {URL}", response=http_response(500)),
      requests.ConnectionError(f"HTTPSConnectionPool: max retries for url: {URL}"),
      requests.Timeout(f"timed out: {URL}"),
      RuntimeError(f"failed: {URL}"),
  ])
  def test_the_reason_never_holds_the_webhook_url(self, error):
    reason, _ = webhook_failure(error, "Discord")
    assert "SECRET-TOKEN" not in reason


class TestRequestWebhook:
  def test_a_successful_request_has_no_failure(self, session):
    assert _request_webhook("POST", URL, "Discord", json={"a": 1}) is None

  def test_it_passes_the_method_url_timeout_and_arguments_on(self, session):
    _request_webhook("POST", URL, "Discord", json={"a": 1})
    assert session.calls == [("POST", URL, {"timeout": 5, "json": {"a": 1}})]

  def test_an_http_error_status_is_a_failure(self, session):
    session.outcome = http_response(500)
    assert _request_webhook("GET", URL, "Discord") == ("Discord answered HTTP 500", True)

  def test_an_exception_from_the_request_is_a_failure(self, session):
    session.outcome = requests.ConnectionError("down")
    assert _request_webhook("GET", URL, "Discord") == ("could not reach Discord", True)


class TestChannelCheckFailed:
  def test_it_is_a_value_error(self):
    assert isinstance(ChannelCheckFailed({"a": ("x", True)}, 1), ValueError)

  def test_it_clears_by_itself_when_every_configured_channel_failed_that_way(self):
    failures = {"a": ("A down", True), "b": ("B down", True)}
    assert ChannelCheckFailed(failures, configured=2).transient is True

  def test_one_failure_that_does_not_clear_makes_it_permanent(self):
    failures = {"a": ("A down", True), "b": ("B refused", False)}
    assert ChannelCheckFailed(failures, configured=2).transient is False

  def test_a_partial_failure_needs_the_user_even_if_it_would_clear(self):
    assert ChannelCheckFailed({"a": ("A down", True)}, configured=2).transient is False

  def test_the_message_is_the_prefix_and_the_reasons(self):
    failures = {"a": ("A down", True), "b": ("B refused", False)}
    assert str(ChannelCheckFailed(failures, 2, "Not delivered")) == "Not delivered: A down; B refused."

  def test_without_a_prefix_the_message_is_just_the_reasons(self):
    assert str(ChannelCheckFailed({"a": ("A down", True)}, 1)) == "A down"

  def test_channels_gives_the_reason_by_field(self):
    failures = {"a": ("A down", True), "b": ("B refused", False)}
    assert ChannelCheckFailed(failures, 2).channels == {"a": "A down", "b": "B refused"}


class TestDiscordPayload:
  def test_the_message_is_one_embed(self):
    payload = discord_payload(SimpleNamespace(type="success", content="Deployed", link=None))

    assert payload == {"embeds": [{"title": "✅ manager example.test", "description": "Deployed", "color": 3066993}]}

  @pytest.mark.parametrize("kind", list(NOTIFICATION_STYLES))
  def test_each_type_has_its_own_emoji_and_color(self, kind):
    embed = discord_payload(SimpleNamespace(type=kind, content="x", link=None))["embeds"][0]

    assert embed["title"].startswith(NOTIFICATION_STYLES[kind]["emoji"])
    assert embed["color"] == NOTIFICATION_STYLES[kind]["color"]

  def test_an_unknown_type_looks_like_info(self):
    embed = discord_payload(SimpleNamespace(type="surprise", content="x", link=None))["embeds"][0]
    assert embed["color"] == NOTIFICATION_STYLES["info"]["color"]

  def test_a_link_is_added_as_a_field(self):
    embed = discord_payload(SimpleNamespace(type="info", content="x", link="https://app.example.test/p"))["embeds"][0]
    assert embed["fields"] == [{"name": "", "value": "https://app.example.test/p", "inline": False}]

  def test_without_a_link_there_are_no_fields(self):
    assert "fields" not in discord_payload(SimpleNamespace(type="info", content="x", link=""))["embeds"][0]


class TestLoadingTheConfig:
  def test_values_come_from_the_notifications_settings(self, configured_notifications):
    assert configured_notifications.get_notification_value("discord_webhook") == URL

  def test_an_unknown_key_gives_the_default(self, notifications):
    assert notifications.get_notification_value("nope", "fallback") == "fallback"

  def test_changed_settings_are_picked_up_on_reload(self, configured_notifications):
    Settings().set("notifications", {"discord_webhook": "https://discord.test/other"})
    assert configured_notifications.get_notification_value("discord_webhook") == URL

    configured_notifications.load_notifications_config()

    assert configured_notifications.get_notification_value("discord_webhook") == "https://discord.test/other"


class TestCheckConfig:
  @pytest.mark.parametrize("config", [{}, {"discord_webhook": ""}, {"discord_webhook": None}])
  def test_without_a_configured_channel_nothing_is_checked(self, notifications, webhook, config):
    assert notifications.check_config(config) == []
    assert webhook.calls == []

  def test_it_returns_the_names_of_the_channels_it_checked(self, notifications, webhook):
    assert notifications.check_config({"discord_webhook": URL}) == ["Discord"]

  def test_with_send_each_channel_gets_a_test_message(self, notifications, webhook):
    notifications.check_config({"discord_webhook": URL}, send=True)

    [(method, url, name, kwargs)] = webhook.calls
    assert (method, url, name) == ("POST", URL, "Discord")
    embed = kwargs["json"]["embeds"][0]
    assert embed["description"] == TEST_NOTIFICATION.content
    assert embed["title"].endswith("manager example.test")

  def test_without_send_the_webhook_is_only_read(self, notifications, webhook):
    notifications.check_config({"discord_webhook": URL}, send=False)
    assert webhook.calls == [("GET", URL, "Discord", {})]

  def test_a_failing_channel_raises_with_the_reason(self, notifications, webhook):
    webhook.failure = ("Discord answered HTTP 404", False)

    with pytest.raises(ChannelCheckFailed) as raised:
      notifications.check_config({"discord_webhook": URL})

    assert str(raised.value) == "The test message was not delivered: Discord answered HTTP 404."
    assert raised.value.channels == {"discord_webhook": "Discord answered HTTP 404"}
    assert raised.value.transient is False

  def test_a_failure_that_clears_by_itself_is_marked_transient(self, notifications, webhook):
    webhook.failure = ("could not reach Discord", True)

    with pytest.raises(ChannelCheckFailed) as raised:
      notifications.check_config({"discord_webhook": URL})

    assert raised.value.transient is True

  def test_without_send_the_message_has_no_prefix(self, notifications, webhook):
    webhook.failure = ("Discord answered HTTP 404", False)

    with pytest.raises(ChannelCheckFailed, match="^Discord answered HTTP 404$"):
      notifications.check_config({"discord_webhook": URL}, send=False)


class TestDispatch:
  def test_a_channel_without_a_webhook_is_skipped(self, notifications, webhook):
    notifications.dispatch({"content": "hello"})
    assert webhook.calls == []

  def test_it_posts_the_notification_to_each_configured_channel(self, configured_notifications, webhook):
    configured_notifications.dispatch({"type": "error", "content": "Deploy failed", "link": "https://app.example.test/p"})

    [(method, url, name, kwargs)] = webhook.calls
    assert (method, url, name) == ("POST", URL, "Discord")
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == f"{NOTIFICATION_STYLES['error']['emoji']} manager example.test"
    assert embed["description"] == "Deploy failed"
    assert embed["color"] == NOTIFICATION_STYLES["error"]["color"]
    assert embed["fields"][0]["value"] == "https://app.example.test/p"

  def test_a_dict_without_a_type_is_info_and_without_a_link_has_none(self, configured_notifications, webhook):
    configured_notifications.dispatch({"content": "hello"})

    embed = webhook.calls[0][3]["json"]["embeds"][0]
    assert embed["color"] == NOTIFICATION_STYLES["info"]["color"]
    assert "fields" not in embed

  def test_an_object_is_used_as_it_is(self, configured_notifications, webhook):
    configured_notifications.dispatch(SimpleNamespace(type="warning", content="Disk is full", link=None))

    embed = webhook.calls[0][3]["json"]["embeds"][0]
    assert embed["description"] == "Disk is full"
    assert embed["color"] == NOTIFICATION_STYLES["warning"]["color"]

  def test_a_failed_delivery_is_logged_not_raised(self, configured_notifications, webhook, caplog):
    webhook.failure = ("Discord answered HTTP 500", True)

    with caplog.at_level(logging.ERROR, logger="services.notification"):
      configured_notifications.dispatch({"content": "hello"})

    assert "Failed to send notification to Discord: Discord answered HTTP 500" in caplog.text

  def test_an_exception_is_logged_not_raised(self, configured_notifications, webhook, caplog):
    webhook.failure = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="services.notification"):
      configured_notifications.dispatch({"content": "hello"})

    assert "Failed to send notification to Discord: boom" in caplog.text
