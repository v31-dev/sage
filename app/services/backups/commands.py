"""The Backup table's writes: the backups of a slot queued and settled, backups deleted by the user and a catalog plan
applied."""
from datetime import datetime

from services.backups.catalog import UNFINISHED, CatalogPlan
from services.backups.updates import backups_changed
from services.db.models import Backup, Container, Volume
from services.db.writes import write
from utils.common import new_id, utcnow


def add_backups(storage_id: str, slot_at: datetime, automatic: bool, keys: list[str],
                volumes: list[Volume] | None = None, container: Container | None = None) -> list[str]:
  """Records the backups of one slot as queued, in the store `storage_id`, each at its key: one of each of `volumes`
  from the container, with its worker, or the platform's when no volumes are given. Their ids, in that order."""
  ids = [new_id() for _ in keys]
  with write():
    for index, (backup_id, key) in enumerate(zip(ids, keys)):
      volume = volumes[index] if volumes else None
      Backup.create(id=backup_id, s3_path=key, storage_id=storage_id, type="application" if volume else "platform",
                    source_volume_id=volume and volume.id, source_volume_name=volume and volume.name,
                    application=volume and volume.application_id, container_id=container and container.id,
                    worker_hostname=container and container.worker_id, slot_at=slot_at, automatic=automatic,
                    status="queued")
  backups_changed(volume.id for volume in volumes or ())
  return ids


def settle_backups(backup_ids: list[str], status: str, reason: str | None = None) -> bool:
  """Sets the status of those of the backups still queued or running, with its reason; whether any was."""
  now = utcnow()
  with write():
    volume_ids = list(Backup.select(Backup.source_volume_id).where(Backup.id.in_(backup_ids)).scalars())
    settled = Backup.update(status=status, reason=reason, updated_at=now).where(
        Backup.id.in_(backup_ids) & Backup.status.in_(UNFINISHED)).execute()
  if settled:
    backups_changed(volume_ids)
  return bool(settled)


def requeue_backups(backup_ids: list[str]) -> bool:
  """Sets the backups queued again, for another attempt at their slot at the same keys; whether any was."""
  now = utcnow()
  with write():
    volume_ids = list(Backup.select(Backup.source_volume_id).where(Backup.id.in_(backup_ids)).scalars())
    requeued = Backup.update(status="queued", reason=None, updated_at=now).where(Backup.id.in_(backup_ids)).execute()
  if requeued:
    backups_changed(volume_ids)
  return bool(requeued)


def cancel_queued_backups(keep: list[str], container_id: str | None = None) -> bool:
  """Settles cancelled the queued backups whose task was replaced or cancelled, all but `keep`: the container's manual
  ones, or the platform's when no container is given. Whether any was."""
  kind = (Backup.container_id == container_id) & ~Backup.automatic if container_id else Backup.type == "platform"
  with write():
    rows = list(Backup.select(Backup.id, Backup.source_volume_id).where(
        kind & (Backup.status == "queued") & Backup.id.not_in(list(keep))))
    if rows:
      Backup.update(status="cancelled", updated_at=utcnow()).where(Backup.id.in_([row.id for row in rows])).execute()
  if rows:
    backups_changed(row.source_volume_id for row in rows)
  return bool(rows)


def mark_deleted(backup_ids: list[str]) -> bool:
  """Marks the backups deleted, those not deleted yet; whether any was."""
  now = utcnow()
  with write():
    volume_ids = list(Backup.select(Backup.source_volume_id).where(Backup.id.in_(backup_ids)).scalars())
    marked = Backup.update(deleted_at=now, updated_at=now).where(
        Backup.id.in_(backup_ids) & Backup.deleted_at.is_null()).execute()
  if marked:
    backups_changed(volume_ids)
  return bool(marked)


def apply_catalog(plan: CatalogPlan) -> bool:
  """Adds, settles and drops the rows of `plan` in one transaction; whether anything changed."""
  now, settled = utcnow(), 0
  with write():
    volume_ids = [insert.get("source_volume_id") for insert in plan.inserts] + list(Backup.select(
        Backup.source_volume_id).where(Backup.id.in_([*plan.settles, *plan.deletes])).scalars())
    if plan.inserts:
      # One insert takes its columns from the first row, and a platform backup has no volume.
      fields = sorted({name for insert in plan.inserts for name in insert})
      Backup.insert_many([{name: insert.get(name) for name in fields} for insert in plan.inserts]).execute()
    for status in set(plan.settles.values()):
      settled += Backup.update(status=status, updated_at=now).where(
          Backup.id.in_([backup_id for backup_id, value in plan.settles.items() if value == status])).execute()
    deleted = Backup.delete().where(Backup.id.in_(plan.deletes)).execute() if plan.deletes else 0
  changed = bool(plan.inserts) or bool(settled) or bool(deleted)
  if changed:
    backups_changed(volume_ids)
  return changed
