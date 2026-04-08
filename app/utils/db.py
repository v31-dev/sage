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


class CleanCharField(CharField):
    def db_value(self, value):
        """Clean the value by lowercasing and removing special characters before saving to DB"""
        if value:
            value = value.lower()
            return re.sub(r"[^a-z0-9]", "", value)
        return value
