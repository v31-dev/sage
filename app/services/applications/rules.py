import re

from croniter import croniter

from values.headers import APPLICATION_OWN_HEADERS, SECURITY_HEADERS

MAX_RESPONSE_HEADERS = 50
MAX_HEADER_VALUE_LENGTH = 4096
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def validate_image(image: str | None) -> str | None:
  if image and not re.fullmatch(r"[A-Za-z0-9][\w.\-/:@]*", image):
    raise ValueError(f"Invalid image reference: {image!r}. Allowed characters: letters, digits, and . _ - / : @")
  return image


def validate_repo(repo: str | None) -> str | None:
  if repo and not re.fullmatch(r"https?://[\w.-]+/[\w.-]+/[\w.-]+\.git(?:#[\w.-]+)?(?::[\w./-]+)?", repo):
    raise ValueError(
        f"Invalid Git repository URL: {repo}. Should be in format 'https://github.com/user/repo.git<?#branch><?:sub-directory>'")
  return repo


def apply_type(config: dict):
  """Clears in `config` what the other type uses: a Docker application builds nothing, a Git one pulls no image."""
  if config["type"] == "docker":
    config.update(repo=None, path="Dockerfile", args=None, build_secrets=None)
  else:
    config["image"] = None


def validate_volume_path(path: str | None) -> str:
  # The container-side mount target: ':' and quotes would break the Compose volume entry.
  if not isinstance(path, str) or not re.fullmatch(r"/[^\n\r:\"]*", path):
    raise ValueError(f"Invalid volume path: {path!r}. Must be an absolute path without ':' or quotes.")
  return path


def validate_backup_cron(expression: str | None) -> str | None:
  """The expression, or None for blank: no scheduled backup."""
  if expression is None or (isinstance(expression, str) and not expression.strip()):
    return None
  if not isinstance(expression, str) or len(expression.split()) != 5:
    raise ValueError("backup_cron must be a 5-part cron expression.")
  if not croniter.is_valid(expression):
    raise ValueError("Invalid backup_cron expression.")
  return expression


def validate_response_headers(headers: dict | None) -> dict | None:
  """The application's changes to its response headers: a security header's new value, spelled as Sage spells it, or
  null for one of `APPLICATION_OWN_HEADERS` the application sends itself, and its own `X-` headers. What holds no
  change is dropped: another name, a value that is null (for any other header), blank, not text or the default. None
  when there are none."""
  if headers is None:
    return None
  if not isinstance(headers, dict):
    raise ValueError("response_headers must be an object of header names to values.")
  defaults = {name.lower(): name for name in SECURITY_HEADERS}
  seen = set()
  changes = {}
  for name, value in headers.items():
    if not isinstance(name, str):
      continue
    known = defaults.get(name.lower())
    if known is None and not name.lower().startswith("x-"):
      continue
    if not HEADER_NAME.fullmatch(name):
      raise ValueError(f"Invalid header name: {name!r}.")
    if name.lower() in seen:
      raise ValueError(f"Header {name} is given twice.")
    seen.add(name.lower())
    if value is None and known in APPLICATION_OWN_HEADERS:
      changes[known] = None
      continue
    if not isinstance(value, str) or not value.strip():
      continue
    if len(value) > MAX_HEADER_VALUE_LENGTH or CONTROL_CHARACTERS.search(value):
      raise ValueError(f"Invalid value for header {name}: one line of at most {MAX_HEADER_VALUE_LENGTH} characters.")
    if value.strip() != SECURITY_HEADERS.get(known):
      changes[known or name] = value.strip()
  if len(changes) > MAX_RESPONSE_HEADERS:
    raise ValueError(f"An application can change at most {MAX_RESPONSE_HEADERS} response headers.")
  return changes or None


def response_headers(changes: dict | None) -> dict[str, str]:
  """What the application's HTTP names send: Sage's security headers with the application's changes over them, less
  those the application sends itself."""
  return {name: value for name, value in {**SECURITY_HEADERS, **(changes or {})}.items() if value is not None}
