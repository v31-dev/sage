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


def strip_env_comment(line: str) -> str:
  """
  The line without its comment, by the rules Docker Compose applies to the same text:
  - a line starting with '#' is all comment
  - an unquoted value ends at a '#' that follows whitespace, so 'abc#def' is one value
  - a value starting with a quote ends at the matching closing quote (a backslash escapes
    the next character), and a comment may follow it; without a closing quote the rest of
    the line is the value
  """
  if line.lstrip().startswith("#"):
    return ""
  key, separator, rest = line.partition("=")
  if not separator:
    return line

  start = len(rest) - len(rest.lstrip())
  quoted = rest[start:start + 1] in ("'", '"')
  first = 0
  if quoted:
    position = start + 1
    while position < len(rest) and rest[position] != rest[start]:
      position += 2 if rest[position] == "\\" else 1
    if position >= len(rest):
      return line
    first = position + 1

  for index in range(first, len(rest)):
    if rest[index] == "#" and ((index > 0 and rest[index - 1].isspace()) or (quoted and index == first)):
      return f"{key}={rest[:index]}"
  return line


def parse_multiline_kv(config, fn, strip_quotes=False):
  """
  Parse multiline key-value pairs from a string in the format:
  KEY1=value1
  KEY2=value2
  ...

  - drops comments as strip_env_comment does
  - skips blank, comment-only and valueless lines
  - splits on first '=' and strips both key and value
  - leaves quoted values intact, unless strip_quotes=True, which removes one
    matching surrounding pair of single/double quotes (e.g. values with spaces)

  Returns a list of results from applying the provided function `fn` to each key-value pair.
  """
  if not config:
    return []

  results = []
  for line in config.splitlines():
    key, separator, value = strip_env_comment(line).partition("=")
    if not separator:
      continue
    key = key.strip()
    value = value.strip()
    if strip_quotes and len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
      value = value[1:-1]
    results.append(fn(key, value))
  return results
