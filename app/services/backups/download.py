"""A backup's file downloaded by the browser straight from the store, so no backup passes through the manager."""
import logging
import re

import config
from services.backups.keys import timestamp_from_key
from services.db.models import Backup, Volume
from services.s3 import S3
from utils.errors import Conflict

logger = logging.getLogger(__name__)


def download_filename(backup: Backup) -> str:
  """A name for the saved file that says what it holds: a platform backup's own file name; a volume archive's
  application, its volume (by the volume's name now, else the one it had when taken) and its time. Only letters,
  digits, dots, dashes and underscores are kept, since the name is put in quotes."""
  if backup.type == "platform":
    name = backup.s3_path.strip("/").split("/")[-1]
  else:
    volume = Volume.get_or_none(Volume.id == backup.source_volume_id)
    application = backup.application
    name = (f"{application.project_id}-{application.name}-{volume.name if volume else backup.source_volume_name}-"
            f"{timestamp_from_key(backup.s3_path)}.tar.gz.enc")
  return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def download_link(backup: Backup) -> dict:
  """A link that downloads the backup's file from the configured store for BROWSER_LINK_SECONDS, under its
  `download_filename`; a Conflict when the backup is in another store, a LookupError when its file is gone."""
  session = S3().session()
  if backup.storage_id != session.identity:
    raise Conflict("This backup is not in the configured store.")
  if not session.key_exists(backup.s3_path, raw_path=True):
    raise LookupError("The backup file is no longer in S3.")
  url = session.create_presigned_download_url(backup.s3_path, expires_in=config.BROWSER_LINK_SECONDS, raw_path=True,
                                              filename=download_filename(backup))
  logger.info(f"Download link for backup {backup.id} ({backup.s3_path}) valid for {config.BROWSER_LINK_SECONDS}s.")
  return {"url": url, "expires_in": config.BROWSER_LINK_SECONDS}
