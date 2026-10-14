from services.base import Base
from services.db import Setting

# Setting key -> its fields. Every field is optional and is set from the UI.
SETTING_DEFINITIONS = {
    "tailscale": ("worker_tag",),
    "s3": ("access_key", "secret_key", "bucket", "endpoint", "path"),
    "notifications": ("discord_webhook",),
    "cloudflare": ("api_token", "account_id", "domain", "admin_email"),
}


class Settings(Base):
  def __init__(self):
    super().__init__()

    self.values = {}
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

      if needs_save:
        setting.value = current_value
        setting.save()

      self.values[key] = setting.value
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

  def is_valid(self, key: str):
    if key not in SETTING_DEFINITIONS:
      raise KeyError(f"Unknown setting key: {key}")

    value = self.get(key)

    for field in SETTING_DEFINITIONS[key]:
      field_value = value.get(field)
      if field_value is None or field_value == "":
        return False

    return True
