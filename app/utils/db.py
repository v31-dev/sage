from peewee import TextField
from cryptography.fernet import Fernet

from utils.common import get_env


cipher = Fernet(get_env('ENCRYPTION_KEY'))

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