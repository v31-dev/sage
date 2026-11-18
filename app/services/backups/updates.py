"""What tells the backups' pages: the platform's list and each volume's."""
from collections.abc import Iterable

from services.db.models import Volume
from services.updates import Updates, UpdateSource


def backups_changed(volume_ids: Iterable[str | None]):
  """Backup rows were written, of those volumes (none for the platform's): the platform's list and those volumes'
  lists are sent again."""
  Updates().changed(UpdateSource.BACKUPS)
  for volume_id in {volume_id for volume_id in volume_ids if volume_id}:
    Updates().changed(UpdateSource.VOLUME_BACKUPS, volume_id)


def store_changed():
  """The configured store changed, and with it which backups every list shows."""
  backups_changed(Volume.select(Volume.id).scalars())
