import inspect
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import botocore.exceptions
import httpx
import pytest

from services.certs import Certs
from services.cloudflare import Cloudflare, CloudflareError
from services.manager import Manager
from services.manager.queue import SCOPES, QueueMixin
from services.manager.settings import APPLYING_TASKS, SETTING_SECTIONS, SettingsMixin, _is_applying
from services.notification import ChannelCheckFailed, Notifications
from services.s3 import S3
from services.settings import SettingCheck, Settings
from services.tailscale import Tailscale
from services.updates import Updates
from tests.services.test_certs import write_certificate
from tests.services.test_cloudflare import ACCOUNT, TUNNELS, ZONE, Api, ok, zone
from utils.executor import active_executor
from utils.queue import TaskQueue

STORE = {"access_key": "a", "secret_key": "a", "bucket": "a", "endpoint": "https://a.example.test", "path": "base"}
WEBHOOK = {"discord_webhook": "https://discord.example.test/api/webhooks/1/x"}


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
      ("converge_worker", {"hostname": "w1", "force": True}, {"tailscale"}),
      ("converge_worker", {"hostname": "w1"}, set()),
      ("converge_worker", {"hostname": "w1", "force": False}, set()),
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


class Node:
  def __init__(self, state="Running", dns_name="manager.tail1234.ts.net."):
    self.state, self.dns_name = state, dns_name

  def status(self):
    return {"BackendState": self.state, "Self": {"HostName": "manager", "DNSName": self.dns_name}}

  def peers(self):
    return ["w1", "w2"]


class TestTailscaleCheck:
  def check(self, register, **node):
    register(Tailscale, Node(**node))
    return SettingsMixin._test_tailscale(SimpleNamespace(), {}, False)

  def test_a_node_named_ts_name_says_so(self, register):
    assert self.check(register) == "The node is running as manager; 2 other machine(s) on the tailnet."

  @pytest.mark.parametrize("dns_name", ["manager-1.tail1234.ts.net.", "renamed.tail1234.ts.net."])
  def test_a_node_under_another_name_says_which(self, register, dns_name):
    machine = dns_name.split(".")[0]

    assert self.check(register, dns_name=dns_name) == (
        f"The node is running as {machine}, not manager (TS_NAME); 2 other machine(s) on the tailnet.")

  def test_a_node_that_is_not_running_fails_the_check(self, register):
    with pytest.raises(ValueError, match=r"not running \(NeedsLogin\)"):
      self.check(register, state="NeedsLogin")


@pytest.fixture
def account(register):
  """Cloudflare connected to an account whose token is active and whose zone is visible; its tunnels are what the test
  lists in `api`."""
  api = Api()
  api.on("GET", "/user/tokens/verify", ok({"status": "active"}))
  api.on("GET", "/zones", ok([zone(ZONE, ACCOUNT)]))
  cloudflare = Cloudflare()
  cloudflare.http.close()
  cloudflare.http = httpx.Client(base_url="https://cloudflare.test", transport=httpx.MockTransport(api.handle))
  cloudflare.zone_id, cloudflare.account_id, cloudflare.tunnel_id = ZONE, ACCOUNT, "tunnel1"
  register(Cloudflare, cloudflare)
  yield api
  cloudflare.http.close()


class TestCloudflareCheck:
  def check(self, account, statuses):
    account.on("GET", TUNNELS, ok([{"id": name, "name": name, "status": status} for name, status in statuses.items()]))
    checked = []
    return SettingsMixin._test_cloudflare(SimpleNamespace(worker_state=SimpleNamespace(check=lambda: checked.append(True))),
                                          {}, False), checked

  def test_every_tunnels_health_is_kept_and_the_workers_states_are_checked_again(self, account):
    message, checked = self.check(account, {
        "sage-example.test-w1": "healthy", "sage-example.test": "degraded", "sage-other.test-w2": "down"})

    assert message == "API token, zone example.test and account verified; tunnel degraded."
    assert (Cloudflare().tunnel_status, checked) == ("degraded", [True])
    assert Cloudflare().tunnel_health["sage-example.test-w1"] == "healthy"

  def test_without_the_platform_tunnel_the_check_fails(self, account):
    with pytest.raises(ValueError, match="The tunnel sage-example.test is not in the account."):
      self.check(account, {"sage-example.test-w1": "healthy"})


class Settled(SettingsMixin, QueueMixin):
  """The settings part of the Manager on a real queue; what it requests of the other parts is recorded."""

  def __init__(self):
    self.pool = ThreadPoolExecutor(max_workers=1)
    self.queue_facts, self.requested = [], []
    self.task_queue = TaskQueue(SCOPES, {"common": self.pool}, record=lambda task, status: None,
                                notify=self._queue_changed)

  def request_backup_discovery(self):
    self.requested.append("request_backup_discovery")

  def queued(self):
    return [(tuple(task["key"]), task["scopes"], task["executor"], task["params"], task["quiet"])
            for task in self.task_queue.snapshot()["queued"]]


@pytest.fixture
def settled(db, thread_connections, register, signals):
  register(Tailscale, Node())
  installed = SimpleNamespace(s3=[], notifications=[])
  register(S3, SimpleNamespace(install=installed.s3.append, check_config=lambda values, write: None))
  register(Notifications, SimpleNamespace(install=installed.notifications.append,
                                          check_config=lambda values, send: ["Discord"]))
  manager = Settled()
  manager.installed = installed
  token = active_executor.set(manager.pool)
  yield manager
  active_executor.reset(token)
  manager.pool.shutdown(wait=False, cancel_futures=True)


class TestSave:
  def test_a_saved_store_is_installed_its_backups_discovered_and_checked_visibly(self, settled):
    Settings().set("s3", STORE)
    settled.settings_saved("s3")
    assert (settled.installed.s3, settled.installed.notifications, settled.requested) == (
        [STORE], [], ["request_backup_discovery"])
    assert settled.queued() == [(("check_s3",), ["settings:s3"], "common", {"key": "s3"}, False)]
    assert settled.applying_tasks()["s3"]["name"] == "check_s3"

  def test_a_cleared_store_is_installed_off_and_not_checked(self, settled):
    Settings().set("s3", dict.fromkeys(STORE, ""))
    settled.settings_saved("s3")
    assert (settled.installed.s3, settled.queued()) == ([dict.fromkeys(STORE, "")], [])

  def test_saved_notifications_are_installed_and_checked_without_a_discovery(self, settled):
    Settings().set("notifications", WEBHOOK)
    settled.settings_saved("notifications")
    assert (settled.installed.notifications, settled.installed.s3, settled.requested) == ([WEBHOOK], [], [])
    assert [key for key, *_ in settled.queued()] == [("check_notifications",)]

  def test_a_second_save_replaces_the_check_still_queued(self, settled):
    Settings().set("s3", STORE)
    settled.settings_saved("s3")
    Settings().set("s3", {**STORE, "bucket": "b"})
    settled.settings_saved("s3")
    assert [key for key, *_ in settled.queued()] == [("check_s3",)]


class TestScheduledChecks:
  def test_every_configured_section_is_checked_quietly(self, settled):
    Settings().set("notifications", WEBHOOK)
    settled.request_settings_checks()
    assert settled.queued() == [
        (("check_tailscale",), ["settings:tailscale"], "common", {"key": "tailscale"}, True),
        (("check_cloudflare",), ["settings:cloudflare"], "common", {"key": "cloudflare"}, True),
        (("check_notifications",), ["settings:notifications"], "common", {"key": "notifications"}, True)]
    assert set(settled.applying_tasks().values()) == {None}

  def test_a_scheduled_check_after_a_save_keeps_it_applying(self, settled):
    Settings().set("s3", STORE)
    settled.settings_saved("s3")
    settled.request_settings_checks()
    assert [(key, quiet) for key, _, _, _, quiet in settled.queued() if key == ("check_s3",)] == [(("check_s3",), False)]
    assert settled.applying_tasks()["s3"] is not None


  def test_its_fact_is_the_task_that_makes_each_section_apply(self, settled):
    Settings().set("s3", STORE)
    settled.settings_saved("s3")
    [fact] = settled.settings_queue_facts()
    assert fact._compute() == (None, None, settled.applying_tasks()["s3"]["task_id"], None)


class TestCheck:
  async def test_a_check_is_recorded_for_the_revision_it_checked(self, settled, signals):
    await settled.check_setting("tailscale")
    check = Settings().check("tailscale")
    assert (check.revision, check.state, check.message) == (
        0, "ok", "The node is running as manager; 2 other machine(s) on the tailnet.")
    assert signals

  @pytest.mark.parametrize("error, state", [(ValueError("refused"), "error"), (client_error(503), "retrying")])
  async def test_a_failed_check_records_whether_it_will_pass_by_itself(self, settled, register, error, state):
    Settings().set("s3", STORE)

    def refuse(values, write):
      raise error

    register(S3, SimpleNamespace(check_config=refuse))
    await settled.check_setting("s3")
    assert (Settings().check("s3").state, Settings().check("s3").message) == (state, str(error))

  async def test_a_notifications_check_names_its_channels_and_a_failing_one_says_why(self, settled, register):
    Settings().set("notifications", WEBHOOK)
    await settled.check_setting("notifications")
    assert Settings().check("notifications").message == "Webhook valid: Discord."

    def refuse(values, send):
      raise ChannelCheckFailed({"discord_webhook": ("Discord answered HTTP 404", False)}, configured=1)

    register(Notifications, SimpleNamespace(check_config=refuse))
    await settled.check_setting("notifications")
    check = Settings().check("notifications")
    assert (check.state, check.channels) == ("error", {"discord_webhook": "Discord answered HTTP 404"})

  async def test_a_check_of_values_saved_over_meanwhile_is_dropped(self, settled, register, signals):
    Settings().set("s3", STORE)
    register(S3, SimpleNamespace(check_config=lambda values, write: Settings().set("s3", {**STORE, "bucket": "b"})))
    await settled.check_setting("s3")
    assert (Settings().check("s3"), signals) == (None, [])


@pytest.fixture
def catalog(db, clock, register):
  clock.set(datetime.now(timezone.utc).replace(tzinfo=None))
  register(Updates, SimpleNamespace(changed=lambda source: None))
  return Certs()


class TestSettingsStatus:
  def status(self, register, applying=None):
    register(Cloudflare, SimpleNamespace(account_id="a", tunnel_id="t", tunnel_name="platform", tunnel_status="healthy"))
    manager = SimpleNamespace(applying_tasks=lambda: {**dict.fromkeys(SETTING_SECTIONS), **(applying or {})},
                              section_configured=lambda key: key != "notifications")
    for key in SETTING_SECTIONS:
      Settings().record_check(key, SettingCheck(Settings().snapshot(key).revision, "ok", "Provider reachable", "now"))
    return SettingsMixin.settings_status(manager)

  def test_settings_report_a_failed_renewal_even_when_the_leaf_remains_valid(self, catalog, register, monkeypatch):
    for certificate in catalog.inventory().values():
      write_certificate(certificate.fullchain_path.parent, certificate.names)
      catalog.reload(certificate)
    catalog.discovery.error = "order rejected"
    monkeypatch.setattr(type(catalog.directory), "read_bytes", lambda self: pytest.fail("settings read certificate files"))
    status = self.status(register)["cloudflare"]
    assert status["state"] == "error"
    assert "order rejected" in status["message"]
    assert status["details"]["certificates"][1]["valid"]
    assert status["details"]["tunnel_status"] == "healthy"

  def test_a_section_applies_while_its_task_is_queued_and_shows_its_check_otherwise(self, catalog, register):
    status = self.status(register, applying={"s3": {"task_id": "t1"}})
    assert (status["s3"]["state"], status["s3"]["task_id"]) == ("applying", "t1")
    assert (status["tailscale"]["state"], status["notifications"]["state"]) == ("ok", "not_configured")
