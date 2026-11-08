import re

from pydantic import AnyHttpUrl


def derive_name(label: str | None) -> str:
  cleaned = re.sub(r"[^a-z0-9]", "", label.lower()) if isinstance(label, str) else ""
  if not re.fullmatch(r"[a-z][a-z0-9]*", cleaned):
    raise ValueError(
        f"{label!r} does not produce a valid name: it must contain letters and digits only, starting with a letter.")
  return cleaned


def validate_name(value: str | None) -> str | None:
  if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]+", value)):
    raise ValueError(f"Invalid name {value!r}: must contain lowercase letters, digits, or dashes.")
  return value


def validate_alphanumeric_name(value: str | None, max_length: int = 255) -> str | None:
  if value is not None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9]*", value):
      raise ValueError(f"Invalid name {value!r}: must be lowercase letters and digits only, starting with a letter.")
    if len(value) > max_length:
      raise ValueError(f"Invalid name {value!r}: at most {max_length} characters.")
  return value


def validate_label(value: str | None) -> str | None:
  if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value)):
    raise ValueError(
        f"Invalid name {value!r}: must be lowercase letters, digits and dashes, at most 63, not starting or ending with a dash.")
  return value


def normalize_origins(value: list[str] | str | None) -> list[str] | None:
  if value is None or value == "":
    return None
  if not isinstance(value, list):
    raise ValueError("Allowed origins must be a list.")
  origins = []
  for origin in value:
    if not isinstance(origin, str):
      raise ValueError("Allowed origins must contain strings.")
    origin = origin.strip()
    if not origin:
      continue
    if origin not in ("*", "null"):
      try:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in origin) or "@" in origin or "\\" in origin:
          raise ValueError()
        url = AnyHttpUrl(origin)
        if url.path not in (None, "/") or url.query is not None or url.fragment is not None or "*" in url.host:
          raise ValueError()
        origin = str(url).rstrip("/")
      except ValueError as error:
        raise ValueError(f"Invalid origin {origin!r}: use an HTTP(S) origin without a path, credentials, query or fragment, or '*'.") from error
    origins.append(origin)
  return list(dict.fromkeys(origins)) or None
