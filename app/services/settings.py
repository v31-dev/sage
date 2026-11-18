import logging
from dataclasses import dataclass

from services.base import Base
from services.db.models import Setting
from services.notification import CHANNELS

logger = logging.getLogger(__name__)

# Setting key -> its fields. Every field is optional and is set from the UI. Tailscale
# and Cloudflare are required env vars instead (entrypoint.sh).
SETTING_DEFINITIONS = {
    "s3": ("access_key", "secret_key", "bucket", "endpoint", "path"),
    "notifications": tuple(CHANNELS),
}

# Sections of independent optional channels: configured once any one field is set. In the
# others the fields belong together, and all of them must be set.
INDEPENDENT_FIELD_SECTIONS = ("notifications",)


@dataclass(frozen=True)
class SettingsSnapshot:
  """A section's saved values at `revision`, which every save and reload of it moves on."""
  key: str
  revision: int
  values: dict


@dataclass(frozen=True)
class SettingCheck:
  """How a check of the section at `revision` went: ok, retrying or error. `channels` says why each failing channel of
  a section of channels failed."""
  revision: int
  state: str
  message: str | None
  checked_at: str
  channels: dict | None = None


def validate_section(key: str, values: dict):
  """The local rules of a section's values, which a save applies before storing them: only the section's fields, text
  or nothing in each, and in a section whose fields belong together, all of them or none."""
  if key not in SETTING_DEFINITIONS:
    raise KeyError(f"Unknown setting key: {key}")
  if unknown := set(values) - set(SETTING_DEFINITIONS[key]):
    raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
  for field in SETTING_DEFINITIONS[key]:
    if values.get(field) is not None and not isinstance(values[field], str):
      raise ValueError(f"{field} must be text.")
  provided = [values.get(field) for field in SETTING_DEFINITIONS[key]]
  if key not in INDEPENDENT_FIELD_SECTIONS and any(provided) and not all(provided):
    raise ValueError("Fill in every field, or leave them all empty.")


def fields_set(key: str, values: dict) -> bool:
  """Whether every field of the section is set in `values`."""
  if key not in SETTING_DEFINITIONS:
    raise KeyError(f"Unknown setting key: {key}")
  return all(values.get(field) not in (None, "") for field in SETTING_DEFINITIONS[key])


def setting_payload(key: str) -> dict:
  """The saved row of the section (Settings creates each section's row when it loads); a LookupError for an unknown
  section."""
  row = Setting.get_or_none(Setting.key == key) if key in SETTING_DEFINITIONS else None
  if row is None:
    raise LookupError("Setting not found.")
  return {"key": row.key, "value": row.value, "created_at": row.created_at, "updated_at": row.updated_at}


def section_status(configured: bool, applying_task_id: str | None, check: SettingCheck | None,
                   certificates: list[dict] | None = None) -> dict:
  """What a settings section shows: not_configured, applying (with the task), or the state of its current check, which
  `certificates` (Cloudflare's) turn into an error when one needs attention."""
  entry = {"state": check and check.state, "message": check and check.message, "task_id": None,
           "checked_at": check and check.checked_at, "details": None, "channels": check and check.channels}
  if not configured:
    entry.update(state="not_configured", message=None, channels=None)
  elif applying_task_id:
    entry.update(state="applying", task_id=applying_task_id)
  elif check is None:
    entry.update(state="applying", message="Checking…")
  elif certificates is not None and check.state == "ok":
    failed = [certificate for certificate in certificates if not certificate["valid"] or certificate["error"]]
    if failed:
      entry.update(state="error", message="Certificates need attention: " + "; ".join(
          f"{', '.join(certificate['names']) or certificate['id']}: {certificate['error'] or 'not valid'}"
          for certificate in failed))
    else:
      entry["message"] += f" Certificates valid until {min(certificate['expiry'] for certificate in certificates)[:10]}."
  return entry


class Settings(Base):
  def __init__(self):
    super().__init__()

    self.values = {}
    self.revisions: dict[str, int] = {}
    self._checks: dict[str, SettingCheck] = {}
    self.load_all()

  def load_all(self):
    # Clean up any settings rows that are no longer defined in SETTING_DEFINITIONS.
    removed = Setting.delete().where(Setting.key.not_in(list(SETTING_DEFINITIONS))).execute()
    if removed:
      logger.info(f"Removed {removed} unused settings row(s).")

    for key in SETTING_DEFINITIONS:
      self.load(key)

  def load(self, key: str):
    with self.lock:
      template = {k: None for k in SETTING_DEFINITIONS.get(key, {})}
      setting, _ = Setting.get_or_create(key=key, defaults={"value": template})
      current_value = setting.value or {}
      needs_save = False

      for field in SETTING_DEFINITIONS[key]:
        if field not in current_value:
          current_value[field] = None
          needs_save = True

      # Fields that aren't defined are unused; they are dropped like undefined rows.
      for field in [field for field in current_value if field not in SETTING_DEFINITIONS[key]]:
        del current_value[field]
        needs_save = True

      if needs_save:
        setting.value = current_value
        setting.save()

      self.values[key] = setting.value
      self.revisions[key] = self.revisions.get(key, 0) + 1
      return dict(self.values[key])

  def get(self, key: str, field: str | None = None, default=None):
    if key not in SETTING_DEFINITIONS:
      raise KeyError(f"Unknown setting key: {key}")

    if field is not None and field not in SETTING_DEFINITIONS[key]:
      raise KeyError(f"Unknown setting field: {field} for key: {key}")

    if key not in self.values or (field and field not in self.values[key]):
      return default

    if field:
      value = self.values[key][field]
      if value is None or value == "":
        return default
      else:
        return self.values[key][field]
    else:
      return dict(self.values[key])

  def set(self, key: str, value: dict):
    '''
    Value can be a subset of fields for the key. Missing keys will be ignored.
    '''
    if key not in SETTING_DEFINITIONS:
      raise KeyError(f"Unknown setting key: {key}")

    if not isinstance(value, dict):
      raise ValueError(f"Value for key {key} must be a dict with fields from: {list(SETTING_DEFINITIONS[key])}")

    subset_value = {k: v for k, v in value.items() if k in SETTING_DEFINITIONS[key]}

    with self.lock:
      setting = Setting.get(Setting.key == key)
      # Reassign rather than mutate in place: with only_save_dirty a nested
      # mutation isn't tracked and would not be persisted.
      new_value = dict(setting.value or {})
      for field, field_value in subset_value.items():
        new_value[field] = field_value
      setting.value = new_value
      setting.save()
      self.values[key] = setting.value
      self.revisions[key] = self.revisions.get(key, 0) + 1

  def snapshot(self, key: str) -> SettingsSnapshot:
    """The section as saved now, to validate, install and check as one; a required env section is revision 0."""
    with self.lock:
      return SettingsSnapshot(key, self.revisions.get(key, 0), dict(self.values.get(key, {})))

  def record_check(self, key: str, check: SettingCheck) -> bool:
    """Keep the check while the section is still at the revision it checked; False when it was saved or reloaded
    meanwhile, and the check is dropped."""
    with self.lock:
      if check.revision != self.revisions.get(key, 0):
        return False
      self._checks[key] = check
      return True

  def check(self, key: str) -> SettingCheck | None:
    """The check of the section as saved now, None when there is none yet."""
    with self.lock:
      check = self._checks.get(key)
      return check if check is not None and check.revision == self.revisions.get(key, 0) else None

  def is_valid(self, key: str, value: dict | None = None):
    """Whether every field is set, in the stored value or the one given."""
    return fields_set(key, self.get(key) if value is None else value)

  def is_configured(self, key: str, value: dict | None = None):
    """Whether the section has what it needs: every field set, or for a section of
    independent channels, at least one."""
    if key not in INDEPENDENT_FIELD_SECTIONS:
      return self.is_valid(key, value)

    if value is None:
      value = self.get(key)

    return any(value.get(field) not in (None, "") for field in SETTING_DEFINITIONS[key])
