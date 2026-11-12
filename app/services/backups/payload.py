"""The backups as their pages show them: the platform's, and each volume's, found by the volume's id."""
from services.db.models import Backup


def backup_payload(backup: Backup) -> dict:
  return {
      "id": backup.id,
      "type": backup.type,
      "s3_path": backup.s3_path,
      "storage_id": backup.storage_id,
      "source_volume_id": backup.source_volume_id,
      "source_volume_name": backup.source_volume_name,
      "application": backup.application_id,
      "created_at": backup.created_at,
      "updated_at": backup.updated_at,
  }


def platform_backups() -> list[dict]:
  """The platform backups, newest first."""
  return [backup_payload(backup) for backup in
          Backup.select().where(Backup.type == "platform").order_by(Backup.created_at.desc(), Backup.id.desc())]


def volume_backups(volume_id: str) -> list[dict]:
  """The backups of the volume, newest first, whatever its name was when each was taken."""
  return [backup_payload(backup) for backup in
          Backup.select().where((Backup.type == "application") & (Backup.source_volume_id == volume_id))
          .order_by(Backup.created_at.desc(), Backup.id.desc())]


def platform_backup(backup_id: int) -> Backup:
  """The platform backup with that id; a LookupError when there is none."""
  backup = Backup.get_or_none((Backup.id == backup_id) & (Backup.type == "platform"))
  if backup is None:
    raise LookupError("Backup not found.")
  return backup


def volume_backup(volume_id: str, backup_id: int) -> Backup:
  """The volume's backup with that id; a LookupError when there is none."""
  backup = Backup.get_or_none((Backup.id == backup_id) & (Backup.type == "application") & (Backup.source_volume_id == volume_id))
  if backup is None:
    raise LookupError("Backup not found.")
  return backup
