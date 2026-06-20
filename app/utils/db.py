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
  def db_value(self, value):
    """Clean the value by lowercasing, keeping alphanumerics and dashes. Must start with a letter."""
    if value:
      value = value.lower()
      # Keep only alphanumerics and dashes
      value = re.sub(r"[^a-z0-9-]", "", value)
    return value
