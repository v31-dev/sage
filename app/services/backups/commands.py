"""The Backup table's writes: a backup uploaded, a backup removed by the user, and a catalog plan applied."""
from services.backups.catalog import CatalogPlan
from services.db.models import Backup
from services.db.writes import get, write
from services.updates import Updates, UpdateSource


def add_backup(key: str, storage_id: str, volume_id: str | None = None, volume_name: str | None = None,
               application_id: int | None = None) -> Backup:
  """Records the archive uploaded at `key` in the store `storage_id`: a volume's, of its application, or the platform's
  when no volume is given."""
  with write():
    backup = Backup.create(s3_path=key, storage_id=storage_id, type="platform" if volume_id is None else "application",
                           source_volume_id=volume_id, source_volume_name=volume_name, application=application_id)
  Updates().changed(UpdateSource.BACKUPS)
  return backup


def remove_backup(backup_id: int) -> Backup:
  """Deletes the backup's row and returns it; its object is the store's, removed by its own task."""
  with write():
    backup = get(Backup, Backup.id == backup_id)
    backup.delete_instance()
  Updates().changed(UpdateSource.BACKUPS)
  return backup


def apply_catalog(plan: CatalogPlan) -> bool:
  """Adds and drops the rows of `plan` in one transaction; whether anything changed."""
  with write():
    if plan.inserts:
      # One insert takes its columns from the first row, and a platform backup has no volume.
      fields = sorted({name for insert in plan.inserts for name in insert})
      Backup.insert_many([{name: insert.get(name) for name in fields} for insert in plan.inserts]).execute()
    deleted = Backup.delete().where(Backup.id.in_(plan.deletes)).execute() if plan.deletes else 0
  changed = bool(plan.inserts) or bool(deleted)
  if changed:
    Updates().changed(UpdateSource.BACKUPS)
  return changed
