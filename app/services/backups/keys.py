"""Where backups are kept in a store, under its base path: platform backups by time, volume archives by the opaque ids
of their volume and of the placement they were taken from."""
import re
from datetime import datetime

from utils.common import OPAQUE_ID

BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
PLATFORM_PREFIX = "backups/platform"
APPLICATIONS_PREFIX = "backups/applications"
VOLUME_ARCHIVE_SUFFIX = ".tar.gz.enc"


def timestamp_from_key(key: str) -> str | None:
  match = re.search(r"\d{8}_\d{6}", key.strip("/").split("/")[-1])
  return match.group(0) if match else None


def is_expired_timestamp(timestamp_str: str, cutoff: datetime) -> bool:
  try:
    return datetime.strptime(timestamp_str, BACKUP_TIMESTAMP_FORMAT) < cutoff
  except Exception:
    return False


def is_expired_backup_key(key: str, cutoff: datetime) -> bool:
  timestamp_str = timestamp_from_key(key)
  return bool(timestamp_str) and is_expired_timestamp(timestamp_str, cutoff)


def volume_archive_key(volume_id: str, container_id: str, timestamp: str) -> str:
  """The archive's key under the base path."""
  return f"{APPLICATIONS_PREFIX}/{volume_id}/{container_id}/{timestamp}{VOLUME_ARCHIVE_SUFFIX}"


def parse_volume_archive(key: str, base_path: str) -> tuple[str, str, str] | None:
  """The volume id, container id and timestamp of a full key under `base_path`, or None for any other key."""
  match = re.fullmatch(rf"{re.escape(base_path.strip('/'))}/{re.escape(APPLICATIONS_PREFIX)}/({OPAQUE_ID})/({OPAQUE_ID})/"
                       rf"(\d{{8}}_\d{{6}}){re.escape(VOLUME_ARCHIVE_SUFFIX)}", key)
  return (match[1], match[2], match[3]) if match else None
