import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import botocore.exceptions
import httpx
import pytest

from services.certs import Certs
from services.cloudflare import CloudflareError
from services.manager import Manager
from services.manager.settings import APPLYING_TASKS, SETTING_SECTIONS, SettingsMixin, _is_applying
from services.notification import ChannelCheckFailed
from services.updates import Updates
from tests.services.test_certs import write_certificate


def describe(error):
  return SettingsMixin.describe_check_error(error)


def client_error(status):
  response = {"Error": {"Code": "Failure", "Message": "nope"}, "ResponseMetadata": {"HTTPStatusCode": status}}
  return botocore.exceptions.ClientError(response, "ListObjectsV2")


class TestDescribeCheckError:
  def test_a_channel_failure_keeps_its_message_and_its_own_transient_flag(self):
    transient = ChannelCheckFailed({"discord_webhook": ("could not reach Discord", True)}, configured=1)
    permanent = ChannelCheckFailed({"discord_webhook": ("Discord answered HTTP 404", False)}, configured=1)

    assert describe(transient) == ("could not reach Discord", True)
    assert describe(permanent) == ("Discord answered HTTP 404", False)

  def test_a_partial_channel_failure_is_not_transient(self):
    error = ChannelCheckFailed({"a": ("A down", True)}, configured=2)
    assert describe(error) == ("A down", False)

  @pytest.mark.parametrize("status, transient", [(503, True), (403, False)])
  def test_an_s3_error_keeps_its_message_and_is_transient_only_for_a_server_error(self, status, transient):
    error = client_error(status)
    assert describe(error) == (str(error), transient)

  @pytest.mark.parametrize("status, transient", [(503, True), (429, True), (403, False)])
  def test_a_cloudflare_error_follows_its_own_flag(self, status, transient):
    error = CloudflareError(status, "reason")
    assert describe(error) == (f"Cloudflare answered HTTP {status}: reason", transient)

  @pytest.mark.parametrize("error", [httpx.ConnectError("failed: https://api.cloudflare.com/client/v4/zones"), httpx.ReadTimeout("slow")])
  def test_a_network_error_reaching_cloudflare_is_transient_and_hides_the_details(self, error):
    assert describe(error) == ("could not reach Cloudflare", True)

  @pytest.mark.parametrize("error", [ValueError("Fill in every S3 field first."), RuntimeError("boom"), KeyError("x")])
  def test_anything_else_is_permanent_with_its_own_message(self, error):
    assert describe(error) == (str(error), False)


class TestIsApplying:
  @pytest.mark.parametrize("name, params, sections", [
      ("check_tailscale", {}, {"tailscale"}),
      ("sync_workers", {"force": True}, {"tailscale"}),
      ("sync_workers", {"check_stamps": True}, {"tailscale"}),
      ("sync_workers", {"force": True, "check_stamps": True, "other": 1}, {"tailscale"}),
      ("sync_workers", {}, set()),
      ("sync_workers", {"force": False}, set()),
      ("sync_workers", {"force": False, "check_stamps": False}, set()),
      ("check_cloudflare", {}, {"cloudflare"}),
      ("renew_certificates", {}, {"cloudflare"}),
      ("check_s3", {}, {"s3"}),
      ("check_notifications", {}, {"notifications"}),
      ("backup_platform", {}, set()),
  ])
  def test_a_task_makes_only_its_own_sections_apply(self, name, params, sections):
    queued = {"name": name, "params": params}
    assert {key for key in APPLYING_TASKS if _is_applying(queued, key)} == sections

  def test_every_settings_section_has_applying_tasks(self):
    assert set(APPLYING_TASKS) == set(SETTING_SECTIONS)

  @pytest.mark.parametrize("key", SETTING_SECTIONS)
  def test_every_section_applies_while_its_own_check_runs(self, key):
    assert _is_applying({"name": f"check_{key}", "params": {"key": key}}, key)

  @pytest.mark.parametrize("key, name, params", [
      (key, name, params) for key, tasks in APPLYING_TASKS.items() for name, params in tasks if name != f"check_{key}"])
  def test_the_other_tasks_are_manager_methods_that_take_those_parameters(self, key, name, params):
    assert set(params) <= set(inspect.signature(getattr(Manager, name)).parameters)


class TestTailscaleCheck:
  def check(self, state="Running", dns_name="manager.tail1234.ts.net."):
    status = {"BackendState": state, "Self": {"HostName": "manager", "DNSName": dns_name}}
    tailscale = SimpleNamespace(status=lambda: status, peers=lambda: ["w1", "w2"])
    return SettingsMixin._test_tailscale(SimpleNamespace(tailscale=tailscale), {}, False)

  def test_a_node_named_ts_name_says_so(self):
    assert self.check() == "The node is running as manager; 2 other machine(s) on the tailnet."

  @pytest.mark.parametrize("dns_name", ["manager-1.tail1234.ts.net.", "renamed.tail1234.ts.net."])
  def test_a_node_under_another_name_says_which(self, dns_name):
    machine = dns_name.split(".")[0]

    assert self.check(dns_name=dns_name) == (
        f"The node is running as {machine}, not manager (TS_NAME); 2 other machine(s) on the tailnet.")

  def test_a_node_that_is_not_running_fails_the_check(self):
    with pytest.raises(ValueError, match=r"not running \(NeedsLogin\)"):
      self.check(state="NeedsLogin")


class TestCloudflareCheck:
  def check(self, statuses, listed=("w1", "w2")):
    cloudflare = SimpleNamespace(
        config={}, check_config=lambda config: None, tunnel_name="sage-example.test",
        tunnels=lambda: {name: {"id": name, "status": status} for name, status in statuses.items()},
        tunnel_status=None, domain="example.test", worker_tunnel_name=lambda hostname: f"sage-example.test-{hostname}")
    pushed = {}
    manager = SimpleNamespace(cloudflare=cloudflare, worker_list=[{"hostname": hostname} for hostname in listed],
                              update_workers=pushed.update)
    message = SettingsMixin._test_cloudflare(manager, {}, False)
    return message, cloudflare.tunnel_status, pushed

  def test_the_platform_tunnels_health_is_kept_and_each_listed_worker_gets_its_own_tunnels(self):
    message, status, pushed = self.check({
        "sage-example.test": "degraded", "sage-example.test-w1": "healthy", "sage-other.test-w2": "down"})

    assert message == "API token, zone example.test and account verified; tunnel degraded."
    assert status == "degraded"
    assert pushed == {"w1": {"tunnel": "healthy"}, "w2": {"tunnel": None}}

  def test_without_the_platform_tunnel_the_check_fails(self):
    with pytest.raises(ValueError, match="The tunnel sage-example.test is not in the account."):
      self.check({"sage-example.test-w1": "healthy"})


@pytest.fixture
def catalog(db, clock, register):
  clock.set(datetime.now(timezone.utc).replace(tzinfo=None))
  register(Updates, SimpleNamespace(changed=lambda source: None))
  return Certs()


class TestCertificateSettings:
  def test_settings_report_a_failed_renewal_even_when_the_leaf_remains_valid(self, catalog, monkeypatch):
    for certificate in catalog.inventory().values():
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      catalog.reload(certificate)
    catalog.discovery.error = "order rejected"
    manager = SimpleNamespace(
        certs=catalog, setting_checks={key: {"state": "ok", "message": "Provider reachable"} for key in SETTING_SECTIONS},
        applying_tasks=lambda: dict.fromkeys(SETTING_SECTIONS), section_configured=lambda key: True,
        cloudflare=SimpleNamespace(account_id="a", tunnel_id="t", tunnel_name="platform", tunnel_status="healthy"))
    monkeypatch.setattr(type(catalog.directory), "read_bytes", lambda self: pytest.fail("settings read certificate files"))
    status = SettingsMixin.settings_status(manager)["cloudflare"]
    assert status["state"] == "error"
    assert "order rejected" in status["message"]
    assert status["details"]["certificates"][1]["valid"]
