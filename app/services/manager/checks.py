import logging
from datetime import datetime

import botocore.exceptions
import cloudflare
import requests

from services.notification import Notifications, webhook_error
from services.settings import SETTING_DEFINITIONS, Settings
from utils.executor import run_in_executor_with_context

logger = logging.getLogger(__name__)

SETTING_SECTIONS = ("tailscale", "cloudflare", "s3", "notifications")

# Failures expected to clear on their own; the periodic check retries them.
TRANSIENT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    botocore.exceptions.ConnectionError,
    botocore.exceptions.ReadTimeoutError,
    cloudflare.APIConnectionError,
    cloudflare.InternalServerError,
    cloudflare.RateLimitError,
)

# While one of these is queued or running, its section shows as applying. sync_workers
# counts only when forced (Resync workers); the 30 s sync runs all the time.
APPLYING_TASKS = {
    "tailscale": ("check_tailscale", "sync_workers"),
    "cloudflare": ("check_cloudflare", "refresh_traefik", "renew_certificates"),
    "s3": ("check_s3",),
    "notifications": ("check_notifications",),
}


class ChecksMixin:
  """Live checks of the settings sections against their providers, and the status the
  settings pages show."""

  def test_setting(self, key: str, value: dict, side_effects: bool = True) -> str:
    """Check a section's values; returns what was verified, raises with the reason
    otherwise. With side_effects, S3 also writes to the bucket and notifications send a
    test message; the periodic check runs without them."""
    # Tailscale and Cloudflare are env vars, so they are checked as configured.
    if key == "tailscale":
      backend_state = self.tailscale.status().get("BackendState")
      if backend_state != "Running":
        raise ValueError(f"The manager's tailnet node is not running ({backend_state}).")
      machines = self.tailscale.get_by_tag(self.tailscale.worker_tag)
      online = sum(machine.online for machine in machines)
      return f"{len(machines)} machine(s) tagged tag:{self.tailscale.worker_tag}, {online} online."

    if key == "cloudflare":
      self.cloudflare.check_config(self.cloudflare.config)
      return f"API token, zone {self.cloudflare.domain} and account verified."

    if key == "s3":
      if not Settings().is_valid("s3", value):
        raise ValueError("Fill in every S3 field first.")
      self.s3.check_config(value, write=side_effects)
      return "Bucket reachable and writable." if side_effects else "Bucket reachable."

    if key == "notifications":
      if not value.get("discord_webhook"):
        raise ValueError("Set a Discord webhook first.")
      if side_effects:
        Notifications().check_config(value)
        return "Test message sent to Discord."
      # Reads the webhook's details without posting to the channel.
      requests.get(value["discord_webhook"], timeout=5).raise_for_status()
      return "Discord webhook is valid."

    raise KeyError(f"Unknown settings section: {key}")

  @staticmethod
  def describe_check_error(error: Exception) -> tuple[str, bool]:
    """The message for a failed check (never a webhook URL, which holds its token) and
    whether the failure is transient."""
    transient = isinstance(error, TRANSIENT_ERRORS)
    message = str(error)
    if isinstance(error, requests.RequestException):
      status = error.response.status_code if error.response is not None else 0
      transient = transient or status >= 500 or status == 429
      message = webhook_error(error, "Discord")
    elif isinstance(error, botocore.exceptions.ClientError):
      transient = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
    elif isinstance(error, cloudflare.APIStatusError):
      errors = error.body.get("errors", []) if isinstance(error.body, dict) else []
      reasons = "; ".join(e.get("message", "") for e in errors if isinstance(e, dict))
      message = f"Cloudflare answered HTTP {error.status_code}" + (f": {reasons}" if reasons else "")
    return message, transient

  def section_configured(self, key: str) -> bool:
    """Tailscale and Cloudflare always are (required env vars); settings when complete."""
    return key not in SETTING_DEFINITIONS or Settings().is_valid(key)

  async def check_setting(self, key: str):
    """Check a section's configuration and record the result for its status."""
    value = Settings().get(key) if key in SETTING_DEFINITIONS else {}
    try:
      message = await run_in_executor_with_context(self.test_setting, key, value, False)
      state = "ok"
    except Exception as e:
      message, transient = self.describe_check_error(e)
      state = "retrying" if transient else "error"
      logger.warning(f"{key} check failed: {message}")
    self.setting_checks[key] = {"state": state, "message": message, "checked_at": datetime.now().isoformat()}

  def settings_status(self) -> dict:
    """Per section: not_configured, applying (with the task), ok, retrying or error."""
    snapshot = self.task_snapshot()
    active = [*snapshot["running"], *snapshot["queued"]]
    status = {}
    for key in SETTING_SECTIONS:
      check = self.setting_checks.get(key) or {}
      entry = {"state": check.get("state"), "message": check.get("message"), "task_id": None,
               "checked_at": check.get("checked_at")}
      applying = next((t for t in active if t["name"] in APPLYING_TASKS[key]
                       and (t["name"] != "sync_workers" or t["params"].get("force") or t["params"].get("check_stamps"))),
                      None)

      if not self.section_configured(key):
        entry.update(state="not_configured", message=None)
      elif applying:
        entry.update(state="applying", task_id=applying["task_id"])
      elif not check:
        # Checked at startup, every 5 minutes and after a save; this is before the first.
        entry.update(state="applying", message="Checking…")
      elif key == "cloudflare" and check["state"] == "ok":
        certificates = (self.certs.manager, self.certs.workers)
        invalid = [name for certificate in certificates if not certificate.is_valid() for name in certificate.names]
        if invalid:
          entry.update(state="error",
                       message=f"No valid certificate for {', '.join(invalid)}. Resync Traefik to issue it.")
        else:
          expiry = min(certificate.expiry() for certificate in certificates)
          entry["message"] += f" Certificates valid until {expiry:%Y-%m-%d}."
      status[key] = entry
    return status
