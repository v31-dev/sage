import logging

import httpx

from services.notification import ChannelCheckFailed, Notifications
from services.settings import SETTING_DEFINITIONS, Settings
from services.updates import Fact, Updates, UpdateSource
from utils.common import get_env, utcnow
from utils.executor import run_in_executor_with_context
from utils.retry import is_transient

logger = logging.getLogger(__name__)

SETTING_SECTIONS = ("tailscale", "cloudflare", "s3", "notifications")


def task(name: str, **params) -> tuple[str, dict]:
  """A task whose run makes its section apply. With params, only a run whose params include
  them counts."""
  return name, params


# While one of these is queued or running, its section shows as applying. The worker sync
# runs every 30 s, so only a forced or stamp-checking run of it counts.
APPLYING_TASKS = {
    "tailscale": (task("check_tailscale"), task("sync_workers", force=True),
                  task("sync_workers", check_stamps=True)),
    "cloudflare": (task("check_cloudflare"), task("refresh_traefik"), task("renew_certificates")),
    "s3": (task("check_s3"),),
    "notifications": (task("check_notifications"),),
}


def _is_applying(queued: dict, key: str) -> bool:
  """Whether a queued or running task (as in task_snapshot) makes the section apply."""
  return any(
      queued["name"] == name and all(queued["params"].get(k) == v for k, v in params.items())
      for name, params in APPLYING_TASKS[key])


class SettingsMixin:
  """Live checks of the settings sections against their providers, and the status the
  settings pages show."""

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
    status = self.tailscale.status()
    if status.get("BackendState") != "Running":
      raise ValueError(f"The manager's tailnet node is not running ({status.get('BackendState')}).")
    peers = f"{len(self.tailscale.peers())} other machine(s) on the tailnet"
    # The machine name, which Tailscale suffixes when another machine has it.
    machine = (status.get("Self", {}).get("DNSName") or "").split(".")[0]
    if machine.lower() != get_env("TS_NAME").lower():
      return f"The node is running as {machine}, not {get_env('TS_NAME')} (TS_NAME); {peers}."
    return f"The node is running as {machine}; {peers}."

  def _test_cloudflare(self, value: dict, side_effects: bool) -> str:
    """Env vars, so checked as configured: token, zone and account, and the platform tunnel's health. Each listed
    worker is given its own tunnel's health, read in the same call."""
    self.cloudflare.check_config(self.cloudflare.config)
    tunnels = self.cloudflare.tunnels()
    if self.cloudflare.tunnel_name not in tunnels:
      raise ValueError(f"The tunnel {self.cloudflare.tunnel_name} is not in the account.")
    self.cloudflare.tunnel_status = tunnels[self.cloudflare.tunnel_name]["status"]
    self.update_workers({
        entry["hostname"]: {"tunnel": (tunnels.get(self.cloudflare.worker_tunnel_name(entry["hostname"])) or {}).get("status")}
        for entry in self.worker_list})
    return f"API token, zone {self.cloudflare.domain} and account verified; tunnel {self.cloudflare.tunnel_status}."

  def _test_s3(self, value: dict, side_effects: bool) -> str:
    if not Settings().is_valid("s3", value):
      raise ValueError("Fill in every S3 field first.")
    self.s3.check_config(value, write=side_effects)
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
    """Check a section's configuration and record the result for its status."""
    value = Settings().get(key) if key in SETTING_DEFINITIONS else {}
    channels = None
    try:
      message = await run_in_executor_with_context(self.test_setting, key, value, False)
      state = "ok"
    except Exception as e:
      message, transient = self.describe_check_error(e)
      state = "retrying" if transient else "error"
      channels = e.channels if isinstance(e, ChannelCheckFailed) else None
      logger.warning(f"{key} check failed: {message}")
    self.setting_checks[key] = {"state": state, "message": message, "channels": channels,
                                "checked_at": utcnow().isoformat()}
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
    """Per section: not_configured, applying (with the task), ok, retrying or error."""
    applying_tasks = self.applying_tasks()
    status = {}
    for key in SETTING_SECTIONS:
      check = self.setting_checks.get(key) or {}
      entry = {"state": check.get("state"), "message": check.get("message"), "task_id": None,
               "checked_at": check.get("checked_at"), "details": None,
               # Why each failing channel failed, by settings field (a section of channels).
               "channels": check.get("channels")}
      applying = applying_tasks[key]

      if not self.section_configured(key):
        entry.update(state="not_configured", message=None, channels=None)
      elif applying:
        entry.update(state="applying", task_id=applying["task_id"])
      elif not check:
        # Checked at startup, every 5 minutes and after a save; this is before the first.
        entry.update(state="applying", message="Checking…")
      elif key == "cloudflare" and check["state"] == "ok":
        certificates = (self.certs.manager, self.certs.workers)
        invalid = [certificate for certificate in certificates if not certificate.is_valid()]
        if invalid:
          # The names it lacks, or all of them when it has expired.
          names = [name for certificate in invalid
                   for name in [name for name in certificate.names if name not in certificate.covered] or certificate.names]
          errors = [certificate.error for certificate in invalid if certificate.error]
          entry.update(state="error", message=f"No valid certificate for {', '.join(names)}"
                       + (f": {' '.join(errors)}" if errors else ". Resync Traefik to issue it."))
        else:
          expiry = min(certificate.expiry() for certificate in certificates)
          entry["message"] += f" Certificates valid until {expiry:%Y-%m-%d}."
      if key == "cloudflare":
        manager_expiry, workers_expiry = self.certs.manager.expiry(), self.certs.workers.expiry()
        entry["details"] = {
            "account_id": self.cloudflare.account_id,
            "tunnel_id": self.cloudflare.tunnel_id,
            "tunnel_name": self.cloudflare.tunnel_name,
            "tunnel_status": self.cloudflare.tunnel_status,
            "manager_certificate_expiry": manager_expiry.isoformat() if manager_expiry else None,
            "workers_certificate_expiry": workers_expiry.isoformat() if workers_expiry else None,
            "workers_certificate_names": [{"name": name, "covered": name in self.certs.workers.covered}
                                          for name in self.certs.workers.names],
            "workers_certificate_error": self.certs.workers.error,
        }
      status[key] = entry
    return status
