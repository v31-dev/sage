import re

from croniter import croniter


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
