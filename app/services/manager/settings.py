"""The settings sections' checks against their providers, the workflow of a Save, and the status the settings pages
show."""
import logging

import httpx

from services.certs import Certs
from services.cloudflare import Cloudflare
from services.notification import ChannelCheckFailed, Notifications
from services.s3 import S3
from services.settings import SETTING_DEFINITIONS, SettingCheck, Settings, section_status
from services.tailscale import Tailscale
from services.updates import Fact, Updates, UpdateSource
from utils.common import get_env, utcnow
from utils.executor import run_in_executor_with_context
from utils.queue import OnConflict
from utils.retry import is_transient

logger = logging.getLogger(__name__)

SETTING_SECTIONS = ("tailscale", "cloudflare", "s3", "notifications")


def task(name: str, *intent: str) -> tuple[str, tuple[str, ...]]:
  """A task whose run makes its section apply. With an intent, only a run whose key ends with it counts."""
  return name, intent


# While one of these is queued or running, its section shows as applying. A worker is set up at every return, so only
# a forced setup (its resync) counts.
APPLYING_TASKS = {
    "tailscale": (task("check_tailscale"), task("converge_worker", "force")),
    "cloudflare": (task("check_cloudflare"), task("renew_certificates")),
    "s3": (task("check_s3"),),
    "notifications": (task("check_notifications"),),
}


def _is_applying(queued: dict, key: str) -> bool:
  """Whether a queued or running task (as in task_snapshot) makes the section apply."""
  return any(
      queued["name"] == name and tuple(queued["key"][len(queued["key"]) - len(intent):]) == intent
      for name, intent in APPLYING_TASKS[key])


class SettingsMixin:
  """Live checks of the settings sections against their providers, the workflow of a Save, and the status the
  settings pages show."""

  def settings_saved(self, key: str):
    """After a Save of the section (the values already validated and saved): its consumer installed from the saved
    snapshot, the backups of a new store discovered, and the section checked as saved."""
    saved = Settings().snapshot(key)
    if key == "s3":
      S3().install(saved.values, saved.revision)
      self.request_backup_discovery()
    elif key == "notifications":
      Notifications().install(saved.values, saved.revision)
    if self.section_configured(key):
      self._request_check(key, quiet=False)

  def request_settings_checks(self):
    """Queues, quietly, the check of every configured section; scheduled every 5 minutes and at startup."""
    for key in SETTING_SECTIONS:
      if self.section_configured(key):
        self._request_check(key, quiet=True)

  def _request_check(self, key: str, quiet: bool):
    self.add_task(task=self.check_setting, key=(f"check_{key}",), scopes={f"settings:{key}"}, executor="common",
                  params={"key": key}, on_conflict=OnConflict.REPLACE, quiet=quiet)

  def test_setting(self, key: str, value: dict, side_effects: bool = True) -> str:
    """Check a section's values; returns what was verified, raises with the reason
    otherwise. With side_effects, S3 also writes to the bucket and notifications send a
    test message; the periodic check runs without them."""
    tests = {
        "tailscale": self._test_tailscale,
        "cloudflare": self._test_cloudflare,
        "s3": self._test_s3,
        "notifications": self._test_notifications,
    }
    if key not in tests:
      raise KeyError(f"Unknown settings section: {key}")
    return tests[key](value, side_effects)

  def _test_tailscale(self, value: dict, side_effects: bool) -> str:
    """Env vars, so checked as configured: the node is up and sees the tailnet, and the name it has there."""
    status = Tailscale().status()
    if status.get("BackendState") != "Running":
      raise ValueError(f"The manager's tailnet node is not running ({status.get('BackendState')}).")
    peers = f"{len(Tailscale().peers())} other machine(s) on the tailnet"
    # The machine name, which Tailscale suffixes when another machine has it.
    machine = (status.get("Self", {}).get("DNSName") or "").split(".")[0]
    if machine.lower() != get_env("TS_NAME").lower():
      return f"The node is running as {machine}, not {get_env('TS_NAME')} (TS_NAME); {peers}."
    return f"The node is running as {machine}; {peers}."

  def _test_cloudflare(self, value: dict, side_effects: bool) -> str:
    """Env vars, so checked as configured: token, zone and account, and the platform tunnel's health; every tunnel's
    health is read in the same call and kept by Cloudflare, so each worker's state shows its own."""
    cloudflare = Cloudflare()
    cloudflare.check_config(cloudflare.config)
    if cloudflare.tunnel_name not in cloudflare.tunnels():
      raise ValueError(f"The tunnel {cloudflare.tunnel_name} is not in the account.")
    self.worker_state.check()
    return f"API token, zone {cloudflare.domain} and account verified; tunnel {cloudflare.tunnel_status}."

  def _test_s3(self, value: dict, side_effects: bool) -> str:
    if not Settings().is_valid("s3", value):
      raise ValueError("Fill in every S3 field first.")
    S3().check_config(value, write=side_effects)
    return "Bucket reachable and writable." if side_effects else "Bucket reachable."

  def _test_notifications(self, value: dict, side_effects: bool) -> str:
    """Every configured channel is checked; one failing fails the section."""
    if not Settings().is_configured("notifications", value):
      raise ValueError("Set a webhook first.")
    names = ", ".join(Notifications().check_config(value, send=side_effects))
    return f"Test message sent to {names}." if side_effects else f"Webhook valid: {names}."

  @staticmethod
  def describe_check_error(error: Exception) -> tuple[str, bool]:
    """The message for a failed check (never a webhook URL, which holds its token) and
    whether the failure is transient."""
    message = "could not reach Cloudflare" if isinstance(error, httpx.TransportError) else str(error)
    return message, is_transient(error)

  def section_configured(self, key: str) -> bool:
    """Tailscale and Cloudflare always are (required env vars); settings when complete."""
    return key not in SETTING_DEFINITIONS or Settings().is_configured(key)

  async def check_setting(self, key: str):
    """Check a section as saved now and record the result for its status, unless it was saved or reloaded meanwhile:
    the result then says nothing about what is saved, and the next check, which that save queued, does."""
    snapshot = Settings().snapshot(key)
    channels = None
    try:
      message = await run_in_executor_with_context(self.test_setting, key, snapshot.values, False)
      state = "ok"
    except Exception as e:
      message, transient = self.describe_check_error(e)
      state = "retrying" if transient else "error"
      channels = e.channels if isinstance(e, ChannelCheckFailed) else None
      logger.warning(f"{key} check failed: {message}")
    if Settings().record_check(key, SettingCheck(snapshot.revision, state, message, utcnow().isoformat(), channels)):
      Updates().changed(UpdateSource.CHECKS)

  def applying_tasks(self) -> dict:
    """Per section, the queued or running task that makes it apply, or None. A quiet task is
    scheduled housekeeping and never does."""
    snapshot = self.task_snapshot()
    active = [task for task in (*snapshot["running"], *snapshot["queued"]) if not task["quiet"]]
    return {key: next((t for t in active if _is_applying(t, key)), None) for key in SETTING_SECTIONS}

  def applying_task_ids(self) -> tuple:
    """The task that makes each section apply, or None, in the order of the sections."""
    return tuple(task and task["task_id"] for task in self.applying_tasks().values())

  def settings_queue_facts(self) -> list[Fact]:
    return [Fact(self.applying_task_ids, UpdateSource.SETTINGS_APPLYING)]

  def settings_status(self) -> dict:
    """Per section: not_configured, applying (with the task), ok, retrying or error; Cloudflare's with the details its
    read-only page shows."""
    applying_tasks, cloudflare, certificates = self.applying_tasks(), Cloudflare(), Certs().status()
    status = {}
    for key in SETTING_SECTIONS:
      applying = applying_tasks[key]
      status[key] = section_status(self.section_configured(key), applying and applying["task_id"], Settings().check(key),
                                   certificates if key == "cloudflare" else None)
    status["cloudflare"]["details"] = {
        "account_id": cloudflare.account_id,
        "tunnel_id": cloudflare.tunnel_id,
        "tunnel_name": cloudflare.tunnel_name,
        "tunnel_status": cloudflare.tunnel_status,
        "certificates": certificates,
    }
    return status
