import logging
import os
import re
from types import SimpleNamespace

import yaml

logger = logging.getLogger(__name__)


def format_yaml(content: str) -> str:
  """Parse and re-serialize a YAML document so templated output is canonical and
  structurally validated before upload. Preserves key order; drops comments."""
  return yaml.safe_dump(yaml.safe_load(content), sort_keys=False, default_flow_style=False)


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def parse_multiline_kv(config, fn, strip_quotes=False):
  """
  Parse multiline key-value pairs from a string in the format:
  KEY1=value1
  KEY2=value2
  ...

  - strips inline comments (ignoring '#' inside quoted values)
  - skips blank or comment-only lines
  - splits on first '=' and strips both key and value
  - leaves quoted values intact, unless strip_quotes=True, which removes one
    matching surrounding pair of single/double quotes (e.g. values with spaces)

  Returns a list of results from applying the provided function `fn` to each key-value pair.
  """
  if not config:
    return []

  results = []
  for line in config.splitlines():
    # inline comment stripping while respecting quotes
    # # can be inside quotes
    in_squote = False
    in_dquote = False
    out_chars = []
    for ch in line:
      if ch == "'" and not in_dquote:
        in_squote = not in_squote
        out_chars.append(ch)
        continue
      if ch == '"' and not in_squote:
        in_dquote = not in_dquote
        out_chars.append(ch)
        continue
      if ch == "#" and not in_squote and not in_dquote:
        break
      out_chars.append(ch)
    line = ''.join(out_chars).strip()
    if not line:
      continue
    if "=" not in line:
      continue
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if strip_quotes and len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
      value = value[1:-1]
    results.append(fn(key, value))
  return results
