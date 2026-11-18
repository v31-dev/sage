from cryptography.fernet import Fernet

_cipher: Fernet | None = None


def initialize_encryption(key: str | None):
  global _cipher
  try:
    _cipher = Fernet(key or "")
  except (ValueError, TypeError) as error:
    raise ValueError("ENCRYPTION_KEY must be a key made with: openssl rand -base64 32 | tr '+/' '-_'") from error


def encrypt(value: str | None) -> str | None:
  if value is None:
    return None
  if _cipher is None:
    raise RuntimeError("Database encryption has not been initialized.")
  return _cipher.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
  if value is None:
    return None
  if _cipher is None:
    raise RuntimeError("Database encryption has not been initialized.")
  return _cipher.decrypt(value.encode()).decode()
