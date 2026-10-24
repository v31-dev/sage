import inspect

import botocore.exceptions
import httpx
import pytest

from services.cloudflare import CloudflareError
from services.manager import Manager
from services.manager.settings import APPLYING_TASKS, SETTING_SECTIONS, SettingsMixin, _is_applying
from services.notification import ChannelCheckFailed


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
      ("refresh_traefik", {}, {"cloudflare"}),
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
