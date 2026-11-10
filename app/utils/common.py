import logging
import os
import secrets
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
  """The current time as naive UTC, which is how Sage stores times and how the UI reads a
  timestamp that has no zone. A test freezes it by patching this module's `datetime`."""
  return datetime.now(timezone.utc).replace(tzinfo=None)


OPAQUE_ID = r"[0-9a-f]{16}"


def new_id() -> str:
  """A fresh opaque identity, matching `OPAQUE_ID`."""
  return secrets.token_hex(8)


def fill(text: str, values: dict) -> str:
  """`text` with `${KEY}` replaced by its value for each key given; any other `${...}`, such as a Compose variable, is
  left as it is."""
  for key, value in values.items():
    text = text.replace("${" + key + "}", str(value))
  return text


def format_yaml(content: str) -> str:
  """Parse and re-serialize a YAML document so templated output is canonical and
  structurally validated before upload. Preserves key order; drops comments."""
  return yaml.safe_dump(yaml.safe_load(content), sort_keys=False, default_flow_style=False)


def dict_to_obj(data):
  """
  Convert a dictionary to an object with named attributes
  """
  return SimpleNamespace(**data)


def get_env(name: str, fallback: str = None) -> str:
  """
  Helper function to get a required environment variable with an optional fallback.
  """
  value = os.getenv(name)

  if value is None or value == "":
    if fallback is not None:
      return fallback
    raise ValueError(f"Environment variable '{name}' is required but not set.")

  return value
