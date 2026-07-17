import json
import re

from cryptography.fernet import Fernet
from peewee import CharField, TextField

from utils.common import get_env

cipher = Fernet(get_env("ENCRYPTION_KEY"))


def validate_multiline_kv(config: str, field_name: str = "config"):
  """
  Validate multiline key/value pairs in the format:
  KEY=value
  KEY2=value2
  ...

  Raises ValueError if any non-empty line is malformed.
  """
  if not config:
    return

  for line_number, line in enumerate(config.splitlines(), start=1):
    # allow inline comments after #
    if "#" in line:
      line = line.split("#", 1)[0]
    line = line.rstrip()
    if not line.strip():
      continue
    if "=" not in line:
      raise ValueError(
          f"{field_name} must contain lines in KEY=value format, invalid line {line_number}: {line!r}"
      )
    key, _, value = line.partition("=")
    # Enforce Docker/env-file compatible key names: letters/underscore then letters/numbers/underscore
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
      raise ValueError(
          f"{field_name} must contain lines in KEY=value format, invalid line {line_number}: {line!r}"
      )
    if value != value.lstrip():
      raise ValueError(
          f"{field_name} must contain lines in KEY=value format, invalid line {line_number}: {line!r}"
      )


class EncryptedTextField(TextField):
  def python_value(self, value):
    """Decrypt when reading from DB"""
    if value is None:
      return None
    return cipher.decrypt(value.encode()).decode()

  def db_value(self, value):
    """Encrypt when writing to DB"""
    if value is None:
      return None
    return cipher.encrypt(value.encode()).decode()


class EncryptedJSONField(TextField):
  def python_value(self, value):
    if value is None:
      return None
    decrypted = cipher.decrypt(value.encode()).decode()
    return json.loads(decrypted)

  def db_value(self, value):
    if value is None:
      return None
    if isinstance(value, (dict, list)):
      value = json.dumps(value)
    return cipher.encrypt(value.encode()).decode()


class JSONField(TextField):
  def python_value(self, value):
    if value is None:
      return None
    return json.loads(value)

  def db_value(self, value):
    if value is None:
      return None
    if isinstance(value, (dict, list)):
      value = json.dumps(value)
    return value


class CleanCharField(CharField):
  """Lowercase alphanumerics and dashes."""

  @staticmethod
  def validate(value):
    if value is not None and not re.fullmatch(r"[a-z0-9-]+", value):
      raise ValueError(
          f"Invalid name {value!r}: must contain lowercase letters, digits, or dashes."
      )
    return value

  def db_value(self, value):
    return self.validate(value)


class AlphaNumericField(CharField):
  """Lowercase alphanumerics only."""

  @staticmethod
  def clean(value):
    """Derive a valid name from free text (e.g. a display label); raises when
    nothing valid can be derived (no letters at all, or digits before the
    first letter)."""
    cleaned = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    if not re.fullmatch(r"[a-z][a-z0-9]*", cleaned):
      raise ValueError(
          f"{value!r} does not produce a valid name: it must contain letters and digits only, starting with a letter."
      )
    return cleaned

  @staticmethod
  def validate(value):
    if value is not None and not re.fullmatch(r"[a-z][a-z0-9]*", value):
      raise ValueError(
          f"Invalid name {value!r}: must be lowercase letters and digits only, starting with a letter."
      )
    return value

  def db_value(self, value):
    return self.validate(value)
