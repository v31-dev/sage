import re


def strip_env_comment(line: str) -> str:
  """Strip comment-only lines and unquoted whitespace-prefixed comments, preserving hashes in values."""
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
  """Apply fn to trimmed key/value pairs, omitting comments and optionally removing surrounding quotes."""
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


def validate_multiline_kv(config: str | None, field_name: str = "config"):
  if config is None:
    return
  if not isinstance(config, str):
    raise ValueError(f"{field_name} must be text.")

  for line_number, line in enumerate(config.splitlines(), start=1):
    line = strip_env_comment(line).rstrip()
    if not line.strip():
      continue
    if "=" not in line:
      raise ValueError(
          f"{field_name} must contain lines in KEY=value format, invalid line {line_number}: {line!r}"
      )
    key, _, value = line.partition("=")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
      raise ValueError(
          f"{field_name} must contain lines in KEY=value format, invalid line {line_number}: {line!r}"
      )
    if value != value.lstrip():
      raise ValueError(
          f"{field_name} must contain lines in KEY=value format, invalid line {line_number}: {line!r}"
      )
