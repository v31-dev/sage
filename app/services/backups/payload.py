"""The backups as their pages show them: the platform's, and each volume's, found by the volume's id; only those in the
configured store, and none the user deleted."""
from services.backups.decisions import platform_restore_refusal
from services.backups.keys import platform_versions
from services.db import migrations
from services.db.models import Backup
from services.s3 import S3
from utils.errors import Conflict


def backup_payload(backup: Backup) -> dict:
  release, schema_version = platform_versions(backup.s3_path) or (None, None)
  return {
      "id": backup.id,
      "type": backup.type,
      "s3_path": backup.s3_path,
      "storage_id": backup.storage_id,
      "source_volume_id": backup.source_volume_id,
      "source_volume_name": backup.source_volume_name,
      "container_id": backup.container_id,
      "worker_hostname": backup.worker_hostname,
      "application": backup.application_id,
      "slot_at": backup.slot_at,
      "automatic": backup.automatic,
      "status": backup.status,
      "reason": backup.reason,
      "created_at": backup.created_at,
      "updated_at": backup.updated_at,
      "release": release,
      "schema_version": schema_version,
      "restore_refusal": platform_restore_refusal(backup.s3_path, len(migrations.STEPS)) if backup.type == "platform" else None,
  }


def _configured_store() -> str | None:
  try:
    return S3().session().identity
  except Conflict:
    return None


def platform_backups() -> list[dict]:
  """The platform backups in the configured store, newest first."""
  return [backup_payload(backup) for backup in
          Backup.select().where((Backup.type == "platform") & (Backup.storage_id == _configured_store())
                                & Backup.deleted_at.is_null())
          .order_by(Backup.created_at.desc(), Backup.id.desc())]


def volume_backups(volume_id: str) -> list[dict]:
  """The backups of the volume in the configured store, newest first, whatever its name was when each was taken."""
  return [backup_payload(backup) for backup in
          Backup.select().where((Backup.type == "application") & (Backup.source_volume_id == volume_id)
                                & (Backup.storage_id == _configured_store()) & Backup.deleted_at.is_null())
          .order_by(Backup.created_at.desc(), Backup.id.desc())]


def platform_backup(backup_id: str) -> Backup:
  """The platform backup with that id, not deleted; a LookupError when there is none."""
  backup = Backup.get_or_none((Backup.id == backup_id) & (Backup.type == "platform") & Backup.deleted_at.is_null())
  if backup is None:
    raise LookupError("Backup not found.")
  return backup


def volume_backup(volume_id: str, backup_id: str) -> Backup:
  """The volume's backup with that id, not deleted; a LookupError when there is none."""
  backup = Backup.get_or_none((Backup.id == backup_id) & (Backup.type == "application") & (Backup.source_volume_id == volume_id)
                              & Backup.deleted_at.is_null())
  if backup is None:
    raise LookupError("Backup not found.")
  return backup
