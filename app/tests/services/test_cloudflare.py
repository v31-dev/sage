import json
import time

import httpx
import pytest

import config
from services.cloudflare import Cloudflare, CloudflareError
from services.tailscale import Tailscale

ACCOUNT = "test-account"
ZONE = "zone1"
TUNNEL = "tunnel1"
TUNNEL_NAME = "example.test-sage"
RECORDS = f"/zones/{ZONE}/dns_records"
TUNNELS = f"/accounts/{ACCOUNT}/cfd_tunnel"


class Api:
  """The Cloudflare API as the tests need it: answers queued per method and path (the last one
  repeats), every request recorded."""

  def __init__(self):
    self.routes = {}
    self.requests = []

  def on(self, method, path, *answers):
    self.routes[(method, path)] = list(answers)

  def handle(self, request):
    self.requests.append(request)
    answers = self.routes.get((request.method, request.url.path))
    if answers is None:
      raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")
    answer = answers.pop(0) if len(answers) > 1 else answers[0]
    if isinstance(answer, Exception):
      raise answer
    return answer

  def sent(self, method, path=None):
    return [request for request in self.requests if request.method == method and (path is None or request.url.path == path)]


def ok(result=None, **extra):
  return httpx.Response(200, json={"success": True, "result": result, **extra})


def fail(status, *messages):
  return httpx.Response(status, json={"success": False, "errors": [{"code": 1, "message": message} for message in messages]})


def body(request):
  return json.loads(request.content)


def zone(zone_id, account_id):
  return {"id": zone_id, "account": {"id": account_id, "name": f"{account_id} account"}}


class FakeTailscale:
  def ip(self):
    return "100.64.0.1"


@pytest.fixture
def api():
  return Api()


@pytest.fixture
def sleeps(monkeypatch):
  delays = []
  monkeypatch.setattr(config, "CLOUDFLARE_RETRY_DELAYS", (1, 2))
  monkeypatch.setattr(time, "sleep", delays.append)
  return delays


@pytest.fixture
def cloudflare(api, sleeps):
  instance = Cloudflare()
  instance.http.close()
  instance.http = httpx.Client(base_url="https://cloudflare.test", transport=httpx.MockTransport(api.handle))
  yield instance
  instance.http.close()


@pytest.fixture
def connected(cloudflare):
  cloudflare.zone_id = ZONE
  cloudflare.account_id = ACCOUNT
  cloudflare.tunnel_id = TUNNEL
  return cloudflare


@pytest.fixture
def healthy(api, register):
  """An account where the token is active, the zone is active and visible, and there is no tunnel or record yet."""
  register(Tailscale, FakeTailscale())
  api.on("GET", "/user/tokens/verify", ok({"status": "active"}))
  api.on("GET", "/zones", ok([zone(ZONE, ACCOUNT)]))
  api.on("GET", TUNNELS, ok([]))
  api.on("POST", TUNNELS, ok({"id": TUNNEL, "name": TUNNEL_NAME}))
  api.on("PUT", f"{TUNNELS}/{TUNNEL}/configurations", ok({}))
  api.on("GET", RECORDS, ok([]))
  api.on("POST", RECORDS, ok({}))
  return api


class TestCloudflareError:
  @pytest.mark.parametrize("status, transient", [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)])
  def test_only_rate_limits_and_server_errors_are_transient(self, status, transient):
    error = CloudflareError(status, "")
    assert error.status_code == status
    assert error.transient is transient

  def test_the_message_has_the_status_and_the_apis_reasons(self):
    assert str(CloudflareError(403, "Invalid token; No access")) == "Cloudflare answered HTTP 403: Invalid token; No access"

  def test_without_reasons_the_message_has_only_the_status(self):
    assert str(CloudflareError(502, "")) == "Cloudflare answered HTTP 502"


class TestConstruction:
  def test_the_names_come_from_the_environment(self):
    cloudflare = Cloudflare()

    assert cloudflare.config == {"api_token": "test-token", "domain": "example.test"}
    assert cloudflare.domain == "example.test"
    assert cloudflare.manager_hostname == "manager.core.example.test"

  def test_it_starts_unconnected(self):
    cloudflare = Cloudflare()
    assert (cloudflare.zone_id, cloudflare.account_id, cloudflare.tunnel_id, cloudflare.tunnel_status) == (
        None, None, None, None)

  def test_the_api_url_comes_from_the_config(self, monkeypatch):
    monkeypatch.setattr(config, "CLOUDFLARE_API_URL", "https://cloudflare.test/v4")
    assert str(Cloudflare().http.base_url).rstrip("/") == "https://cloudflare.test/v4"

  @pytest.mark.parametrize("variable", ["CLOUDFLARE_API_TOKEN", "DOMAIN", "TS_NAME"])
  def test_a_missing_variable_is_named_in_the_error(self, monkeypatch, variable):
    monkeypatch.delenv(variable)
    with pytest.raises(Exception, match=variable):
      Cloudflare()


class TestRequest:
  def test_it_authenticates_with_the_configured_token(self, cloudflare, api):
    api.on("GET", "/zones", ok([]))
    cloudflare.request("GET", "/zones")
    assert api.requests[0].headers["Authorization"] == "Bearer test-token"

  def test_a_given_token_replaces_the_configured_one(self, cloudflare, api):
    api.on("GET", "/zones", ok([]))
    cloudflare.request("GET", "/zones", token="candidate")
    assert api.requests[0].headers["Authorization"] == "Bearer candidate"

  def test_params_and_a_json_body_are_passed_on(self, cloudflare, api):
    api.on("PUT", "/thing", ok({}))

    cloudflare.request("PUT", "/thing", params={"name": "a"}, json={"x": 1})

    request = api.requests[0]
    assert request.url.params["name"] == "a"
    assert body(request) == {"x": 1}

  @pytest.mark.parametrize("status", [429, 500, 502, 503])
  def test_a_transient_status_is_retried_after_each_delay(self, cloudflare, api, sleeps, status):
    api.on("GET", "/zones", fail(status, "busy"), fail(status, "busy"), ok([]))

    assert cloudflare.request("GET", "/zones")["result"] == []

    assert sleeps == [1, 2]
    assert len(api.requests) == 3

  def test_it_gives_up_when_the_retries_run_out(self, cloudflare, api, sleeps):
    api.on("GET", "/zones", fail(503, "unavailable"))

    with pytest.raises(CloudflareError, match="HTTP 503: unavailable") as raised:
      cloudflare.request("GET", "/zones")

    assert raised.value.transient is True
    assert sleeps == [1, 2]
    assert len(api.requests) == 3

  @pytest.mark.parametrize("status", [400, 401, 403, 404])
  def test_any_other_status_fails_at_once(self, cloudflare, api, sleeps, status):
    api.on("GET", "/zones", fail(status, "no"))

    with pytest.raises(CloudflareError) as raised:
      cloudflare.request("GET", "/zones")

    assert raised.value.status_code == status
    assert sleeps == []
    assert len(api.requests) == 1

  def test_the_error_carries_every_message_the_api_gave(self, cloudflare, api):
    api.on("POST", "/zones", fail(400, "Invalid name", "Invalid content"))

    with pytest.raises(CloudflareError) as raised:
      cloudflare.request("POST", "/zones")

    assert str(raised.value) == "Cloudflare answered HTTP 400: Invalid name; Invalid content"

  @pytest.mark.parametrize("response", [
      httpx.Response(400, text="<html>bad gateway</html>"),
      httpx.Response(400, json={"errors": None}),
      httpx.Response(400, json={}),
  ])
  def test_an_error_without_messages_still_raises(self, cloudflare, api, response):
    api.on("GET", "/zones", response)

    with pytest.raises(CloudflareError) as raised:
      cloudflare.request("GET", "/zones")

    assert str(raised.value) == "Cloudflare answered HTTP 400"

  def test_a_network_error_is_retried(self, cloudflare, api, sleeps):
    api.on("GET", "/zones", httpx.ConnectError("down"), ok([]))

    assert cloudflare.request("GET", "/zones")["result"] == []

    assert sleeps == [1]

  def test_a_network_error_that_persists_is_raised(self, cloudflare, api, sleeps):
    api.on("GET", "/zones", httpx.ConnectError("down"))

    with pytest.raises(httpx.ConnectError):
      cloudflare.request("GET", "/zones")

    assert sleeps == [1, 2]
    assert len(api.requests) == 3


class TestListAll:
  def test_a_single_page_is_returned_whole(self, cloudflare, api):
    api.on("GET", "/things", ok([1, 2], result_info={"total_pages": 1}))

    assert cloudflare.list_all("/things") == [1, 2]

    assert len(api.requests) == 1
    assert dict(api.requests[0].url.params) == {"page": "1", "per_page": "100"}

  def test_every_page_is_collected_in_order(self, cloudflare, api):
    info = {"total_pages": 3}
    api.on("GET", "/things", ok([1], result_info=info), ok([2], result_info=info), ok([3], result_info=info))

    assert cloudflare.list_all("/things") == [1, 2, 3]

    assert [request.url.params["page"] for request in api.requests] == ["1", "2", "3"]

  def test_a_response_without_page_info_is_one_page(self, cloudflare, api):
    api.on("GET", "/things", ok([1]))

    assert cloudflare.list_all("/things") == [1]

    assert len(api.requests) == 1

  def test_extra_params_are_sent_with_every_page(self, cloudflare, api):
    info = {"total_pages": 2}
    api.on("GET", "/things", ok([1], result_info=info), ok([2], result_info=info))

    cloudflare.list_all("/things", name="a")

    assert [request.url.params["name"] for request in api.requests] == ["a", "a"]


class TestConnect:
  def test_it_finds_the_zone_and_the_tunnel(self, cloudflare, healthy):
    cloudflare.connect()

    assert cloudflare.zone_id == ZONE
    assert cloudflare.tunnel_id == TUNNEL

  def test_the_account_is_the_one_holding_the_zone(self, cloudflare, healthy):
    cloudflare.connect()
    assert cloudflare.account_id == ACCOUNT
    assert healthy.sent("POST", TUNNELS)

  def test_the_zone_is_looked_up_by_the_domain_and_must_be_active(self, cloudflare, healthy):
    cloudflare.connect()
    params = healthy.sent("GET", "/zones")[0].url.params
    assert (params["name"], params["status"]) == ("example.test", "active")

  def test_it_publishes_the_managers_address_in_dns(self, cloudflare, healthy):
    cloudflare.connect()

    records = [body(request) for request in healthy.sent("POST", RECORDS)]
    assert {"name": "manager.core.example.test", "content": "100.64.0.1", "type": "A", "ttl": 60, "comment": "sage-manager"} in records

  def test_it_routes_the_wildcard_through_the_tunnel(self, cloudflare, healthy):
    cloudflare.connect()

    records = [body(request) for request in healthy.sent("POST", RECORDS)]
    assert {"name": "*.example.test", "content": f"{TUNNEL}.cfargotunnel.com", "type": "CNAME", "proxied": True,
            "comment": TUNNEL_NAME} in records

  def test_when_already_connected_it_does_nothing(self, connected, api):
    connected.connect()
    assert api.requests == []

  def test_an_inactive_token_is_refused(self, cloudflare, healthy):
    healthy.on("GET", "/user/tokens/verify", ok({"status": "disabled"}))

    with pytest.raises(ValueError, match="token is not active"):
      cloudflare.connect()

    assert cloudflare.zone_id is None
    assert healthy.sent("GET", "/zones") == []

  def test_a_zone_the_token_cannot_see_is_refused(self, cloudflare, healthy):
    healthy.on("GET", "/zones", ok([]))

    with pytest.raises(ValueError, match="No active zone for example.test"):
      cloudflare.connect()

    assert cloudflare.zone_id is None

  def test_two_active_zones_are_refused_rather_than_guessed(self, cloudflare, healthy):
    healthy.on("GET", "/zones", ok([zone("z1", "a1"), zone("z2", "a2")]))

    with pytest.raises(ValueError, match="sees 2 active zones for example.test"):
      cloudflare.connect()

    assert (cloudflare.zone_id, cloudflare.account_id) == (None, None)
    assert healthy.sent("POST", TUNNELS) == []

  @pytest.mark.parametrize("answer", [{"id": ZONE}, {"id": ZONE, "account": None}, {"id": ZONE, "account": {"name": "n"}}])
  def test_a_zone_that_does_not_name_its_account_is_refused(self, cloudflare, healthy, answer):
    healthy.on("GET", "/zones", ok([answer]))

    with pytest.raises(ValueError, match="did not say which account holds the zone for example.test"):
      cloudflare.connect()

    assert cloudflare.zone_id is None

  def test_a_failure_after_the_zone_leaves_it_unconnected_so_the_next_use_retries(self, cloudflare, healthy):
    healthy.on("POST", TUNNELS, fail(400, "not allowed"))

    with pytest.raises(CloudflareError):
      cloudflare.connect()
    assert (cloudflare.zone_id, cloudflare.account_id) == (None, None)

    healthy.on("POST", TUNNELS, ok({"id": TUNNEL, "name": TUNNEL_NAME}))
    cloudflare.connect()
    assert (cloudflare.zone_id, cloudflare.account_id) == (ZONE, ACCOUNT)


class TestCheckConfig:
  VALUES = {"api_token": "candidate", "domain": "other.test"}

  @pytest.fixture
  def candidate(self, api):
    api.on("GET", "/user/tokens/verify", ok({"status": "active"}))
    api.on("GET", "/zones", ok([zone("z", "other-account")]))
    api.on("GET", "/accounts/other-account/cfd_tunnel", ok([]))
    return api

  @pytest.mark.parametrize("values, named", [
      ({}, "api_token, domain"),
      ({"domain": "d"}, "api_token"),
      ({"api_token": "t", "domain": ""}, "domain"),
      ({"api_token": None, "domain": "d"}, "api_token"),
  ])
  def test_every_field_is_mandatory(self, cloudflare, values, named):
    with pytest.raises(ValueError, match=f"Missing values for: {named}\\."):
      cloudflare.check_config(values)

  def test_it_checks_the_token_the_zone_and_the_tunnel_access_with_the_given_values(self, cloudflare, candidate):
    cloudflare.check_config(self.VALUES)

    assert [(request.method, request.url.path) for request in candidate.requests] == [
        ("GET", "/user/tokens/verify"), ("GET", "/zones"), ("GET", "/accounts/other-account/cfd_tunnel")]
    assert {request.headers["Authorization"] for request in candidate.requests} == {"Bearer candidate"}
    assert (candidate.requests[1].url.params["name"], candidate.requests[1].url.params["status"]) == ("other.test", "active")

  def test_an_inactive_token_is_refused(self, cloudflare, candidate):
    candidate.on("GET", "/user/tokens/verify", ok({"status": "expired"}))
    with pytest.raises(ValueError, match="The API token is not active."):
      cloudflare.check_config(self.VALUES)

  def test_a_domain_without_an_active_visible_zone_is_refused(self, cloudflare, candidate):
    candidate.on("GET", "/zones", ok([]))
    with pytest.raises(ValueError, match="No active zone for other.test is visible to this API token."):
      cloudflare.check_config(self.VALUES)

  def test_no_tunnel_access_for_the_accounts_of_the_zone_raises_the_apis_error(self, cloudflare, candidate):
    candidate.on("GET", "/accounts/other-account/cfd_tunnel", fail(403, "Authentication error"))
    with pytest.raises(CloudflareError, match="HTTP 403: Authentication error"):
      cloudflare.check_config(self.VALUES)


class TestDnsRecords:
  def test_only_records_with_exactly_that_name_are_returned(self, connected, api):
    wanted = {"id": "1", "name": "a.example.test", "content": "1.1.1.1"}
    api.on("GET", RECORDS, ok([wanted, {"id": "2", "name": "b.a.example.test", "content": "2.2.2.2"}]))

    assert connected.get_dns_records("a.example.test") == [wanted]
    assert api.requests[0].url.params["name"] == "a.example.test"

  def test_it_connects_first(self, cloudflare, healthy):
    cloudflare.get_dns_records("a.example.test")
    assert cloudflare.zone_id == ZONE

  def test_delete_one_removes_the_record_with_that_content(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "1", "name": "a.example.test", "content": "1.1.1.1"},
                               {"id": "2", "name": "a.example.test", "content": "2.2.2.2"}]))
    api.on("DELETE", f"{RECORDS}/2", ok({"id": "2"}))

    connected.delete_dns_record("a.example.test", "2.2.2.2")

    assert [request.url.path for request in api.sent("DELETE")] == [f"{RECORDS}/2"]

  def test_delete_one_with_no_such_content_deletes_nothing(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "1", "name": "a.example.test", "content": "1.1.1.1"}]))

    connected.delete_dns_record("a.example.test", "9.9.9.9")

    assert api.sent("DELETE") == []

  def test_delete_all_removes_every_record_at_the_name(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "1", "name": "a.example.test", "type": "A"},
                               {"id": "2", "name": "a.example.test", "type": "TXT"}]))
    api.on("DELETE", f"{RECORDS}/1", ok({}))
    api.on("DELETE", f"{RECORDS}/2", ok({}))

    connected.delete_dns_records("a.example.test")

    assert [request.url.path for request in api.sent("DELETE")] == [f"{RECORDS}/1", f"{RECORDS}/2"]

  def test_delete_all_can_be_limited_to_one_type(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "1", "name": "a.example.test", "type": "A"},
                               {"id": "2", "name": "a.example.test", "type": "TXT"}]))
    api.on("DELETE", f"{RECORDS}/2", ok({}))

    connected.delete_dns_records("a.example.test", type="TXT")

    assert [request.url.path for request in api.sent("DELETE")] == [f"{RECORDS}/2"]

  def test_delete_all_with_no_records_deletes_nothing(self, connected, api):
    api.on("GET", RECORDS, ok([]))

    connected.delete_dns_records("a.example.test")

    assert api.sent("DELETE") == []


class TestCreateDnsRecord:
  def test_a_new_record_is_created(self, connected, api):
    api.on("GET", RECORDS, ok([]))
    api.on("POST", RECORDS, ok({}))

    connected.create_dns_record("a.example.test", "1.1.1.1", comment="mine", type="TXT")

    assert [body(request) for request in api.sent("POST")] == [{"name": "a.example.test", "content": "1.1.1.1", "type": "TXT", "comment": "mine"}]

  def test_an_a_record_gets_a_ttl_of_60_unless_one_is_given(self, connected, api):
    api.on("GET", RECORDS, ok([]))
    api.on("POST", RECORDS, ok({}))

    connected.create_dns_record("a.example.test", "1.1.1.1", type="A")
    connected.create_dns_record("b.example.test", "1.1.1.1", type="A", ttl=300)

    assert [body(request)["ttl"] for request in api.sent("POST")] == [60, 300]

  def test_other_record_types_get_no_ttl(self, connected, api):
    api.on("GET", RECORDS, ok([]))
    api.on("POST", RECORDS, ok({}))

    connected.create_dns_record("a.example.test", "target.example.test", type="CNAME", proxied=True)

    assert body(api.sent("POST")[0]) == {"name": "a.example.test", "content": "target.example.test", "type": "CNAME", "proxied": True}

  def test_a_record_with_different_content_is_updated_not_duplicated(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "7", "name": "a.example.test", "content": "1.1.1.1", "comment": "mine"}]))
    api.on("PUT", f"{RECORDS}/7", ok({}))

    connected.create_dns_record("a.example.test", "2.2.2.2", comment="mine", type="A")

    assert api.sent("POST") == []
    [update] = api.sent("PUT")
    assert body(update) == {"name": "a.example.test", "content": "2.2.2.2", "type": "A", "ttl": 60, "comment": "mine"}

  def test_a_record_that_is_already_right_is_left_alone(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "7", "name": "a.example.test", "content": "1.1.1.1", "comment": "mine"}]))

    connected.create_dns_record("a.example.test", "1.1.1.1", comment="mine", type="A")

    assert api.sent("POST") == []
    assert api.sent("PUT") == []

  def test_a_record_with_another_comment_is_not_ours_so_a_new_one_is_created(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "7", "name": "a.example.test", "content": "1.1.1.1", "comment": "someone-else"}]))
    api.on("POST", RECORDS, ok({}))

    connected.create_dns_record("a.example.test", "2.2.2.2", comment="mine", type="A")

    assert len(api.sent("POST")) == 1
    assert api.sent("PUT") == []

  def test_without_a_comment_any_record_at_the_name_counts(self, connected, api):
    api.on("GET", RECORDS, ok([{"id": "7", "name": "a.example.test", "content": "1.1.1.1", "comment": "someone-else"}]))
    api.on("PUT", f"{RECORDS}/7", ok({}))

    connected.create_dns_record("a.example.test", "2.2.2.2", type="A")

    assert api.sent("POST") == []
    assert len(api.sent("PUT")) == 1


class TestCreateTunnel:
  def test_a_missing_tunnel_is_created(self, connected, healthy):
    connected.create_tunnel(TUNNEL_NAME)

    assert [body(request) for request in healthy.sent("POST", TUNNELS)] == [{"name": TUNNEL_NAME, "config_src": "cloudflare"}]

  def test_an_existing_tunnel_is_reused(self, connected, healthy):
    healthy.on("GET", TUNNELS, ok([{"id": "existing", "name": TUNNEL_NAME}]))
    healthy.on("PUT", f"{TUNNELS}/existing/configurations", ok({}))

    assert connected.create_tunnel(TUNNEL_NAME) == "existing"

    assert healthy.sent("POST", TUNNELS) == []

  def test_a_tunnel_with_another_name_is_not_reused(self, connected, healthy):
    healthy.on("GET", TUNNELS, ok([{"id": "other", "name": "something-else"}]))

    assert connected.create_tunnel(TUNNEL_NAME) == TUNNEL

    assert len(healthy.sent("POST", TUNNELS)) == 1

  def test_only_live_tunnels_of_that_name_are_listed(self, connected, healthy):
    connected.create_tunnel(TUNNEL_NAME)

    params = healthy.sent("GET", TUNNELS)[0].url.params
    assert (params["name"], params["is_deleted"]) == (TUNNEL_NAME, "false")

  def test_the_ingress_sends_the_wildcard_to_traefik_and_everything_else_to_404(self, connected, healthy):
    connected.create_tunnel(TUNNEL_NAME)

    [push] = healthy.sent("PUT", f"{TUNNELS}/{TUNNEL}/configurations")
    assert body(push) == {"config": {"ingress": [
        {"hostname": "*.example.test", "service": "http://traefik:80"},
        {"service": "http_status:404"},
    ]}}

  def test_the_wildcard_record_points_at_the_tunnel(self, connected, healthy):
    connected.create_tunnel(TUNNEL_NAME)

    assert body(healthy.sent("POST", RECORDS)[0]) == {
        "name": "*.example.test", "content": f"{TUNNEL}.cfargotunnel.com", "type": "CNAME", "proxied": True, "comment": TUNNEL_NAME}

  def test_it_returns_the_tunnel_id(self, connected, healthy):
    assert connected.create_tunnel(TUNNEL_NAME) == TUNNEL


class TestGetTunnelToken:
  def test_it_uses_the_connected_tunnel_by_default(self, connected, api):
    api.on("GET", f"{TUNNELS}/{TUNNEL}/token", ok("the-token"))

    token = connected.get_tunnel_token()

    assert (token.id, token.token) == (TUNNEL, "the-token")

  def test_another_tunnel_can_be_asked_for(self, connected, api):
    api.on("GET", f"{TUNNELS}/other/token", ok("other-token"))

    token = connected.get_tunnel_token("other")

    assert (token.id, token.token) == ("other", "other-token")

  def test_it_connects_first(self, cloudflare, healthy):
    healthy.on("GET", f"{TUNNELS}/{TUNNEL}/token", ok("the-token"))

    assert cloudflare.get_tunnel_token().token == "the-token"

    assert cloudflare.zone_id == ZONE
