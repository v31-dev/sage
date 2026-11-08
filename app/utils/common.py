import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
  """The current time as naive UTC, which is how Sage stores times and how the UI reads a
  timestamp that has no zone. A test freezes it by patching this module's `datetime`."""
  return datetime.now(timezone.utc).replace(tzinfo=None)


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
