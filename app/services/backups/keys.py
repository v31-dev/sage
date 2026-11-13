"""Where backups are kept in a store, under its base path: each archive named by when it was taken and its own opaque
id, a platform backup's under the platform's prefix, a volume archive's under the opaque ids of its volume and of the
placement it was taken from."""
import hashlib
import re
from datetime import datetime

from utils.common import OPAQUE_ID, new_id

BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
PLATFORM_PREFIX = "backups/platform"
APPLICATIONS_PREFIX = "backups/applications"
PLATFORM_BACKUP_SUFFIX = ".db.enc"
VOLUME_ARCHIVE_SUFFIX = ".tar.gz.enc"
ARCHIVE_NAME = rf"(\d{{8}}_\d{{6}})_{OPAQUE_ID}"


def store_scope(storage_id: str) -> str:
  """The queue's scope of the store, over every archive in it."""
  return f"backup:store:{storage_id}"


def archive_write_scope(storage_id: str, part: str) -> str:
  """The queue's scope of the archives written for `part` (a container's id, or `platform`)."""
  return f"{store_scope(storage_id)}:write:{part}"


def archive_scope(storage_id: str, s3_path: str) -> str:
  """The queue's scope of one archive in its store, which a restore from it and its deletion share."""
  return f"{store_scope(storage_id)}:archive:{hashlib.sha256(s3_path.encode()).hexdigest()[:16]}"


def archive_name(at: datetime) -> str:
  """A new archive's name, from when it was taken: unique even for archives taken in the same second."""
  return f"{at.strftime(BACKUP_TIMESTAMP_FORMAT)}_{new_id()}"


def timestamp_from_key(key: str) -> str | None:
  """When the platform backup or volume archive at `key` was taken, as named; None for any other key."""
  match = re.fullmatch(rf"{ARCHIVE_NAME}(?:{re.escape(PLATFORM_BACKUP_SUFFIX)}|{re.escape(VOLUME_ARCHIVE_SUFFIX)})",
                       key.strip("/").split("/")[-1])
  return match[1] if match else None


def taken_at(key: str) -> str:
  """When the backup at `key` was taken, from its key, as people read it."""
  timestamp = timestamp_from_key(key)
  return f"{datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT):%Y-%m-%d %H:%M} UTC" if timestamp else "an unknown time"


def is_expired_timestamp(timestamp_str: str, cutoff: datetime) -> bool:
  try:
    return datetime.strptime(timestamp_str, BACKUP_TIMESTAMP_FORMAT) < cutoff
  except Exception:
    return False


def is_expired_backup_key(key: str, cutoff: datetime) -> bool:
  timestamp_str = timestamp_from_key(key)
  return bool(timestamp_str) and is_expired_timestamp(timestamp_str, cutoff)


def volume_archive_key(volume_id: str, container_id: str, name: str) -> str:
  """The key under the base path of the archive `name` (`archive_name`)."""
  return f"{APPLICATIONS_PREFIX}/{volume_id}/{container_id}/{name}{VOLUME_ARCHIVE_SUFFIX}"


def parse_volume_archive(key: str, base_path: str) -> tuple[str, str, str] | None:
  """The volume id, container id and timestamp of a full key under `base_path`, or None for any other key."""
  match = re.fullmatch(rf"{re.escape(base_path.strip('/'))}/{re.escape(APPLICATIONS_PREFIX)}/({OPAQUE_ID})/({OPAQUE_ID})/"
                       rf"{ARCHIVE_NAME}{re.escape(VOLUME_ARCHIVE_SUFFIX)}", key)
  return (match[1], match[2], match[3]) if match else None
