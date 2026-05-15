import json
import re

from cryptography.fernet import Fernet
from peewee import CharField, TextField

from utils.common import get_env

cipher = Fernet(get_env("ENCRYPTION_KEY"))


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


class CleanCharField(CharField):
  def db_value(self, value):
    """Clean the value by lowercasing, keeping alphanumerics and dashes. Must start with a letter."""
    if value:
      value = value.lower()
      # Keep only alphanumerics and dashes
      value = re.sub(r"[^a-z0-9-]", "", value)
    return value
