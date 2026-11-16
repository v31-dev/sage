"""The Backup table as a mirror of the configured store: what to add, settle and drop after listing it."""
from dataclasses import dataclass, field
from datetime import datetime

from services.backups.keys import BACKUP_TIMESTAMP_FORMAT, parse_volume_archive, timestamp_from_key

UNFINISHED = ("queued", "running")


@dataclass(frozen=True)
class CatalogRow:
  id: str
  s3_path: str
  storage_id: str
  status: str
  deleted: bool
  updated_at: datetime


@dataclass(frozen=True)
class CatalogPlan:
  """Rows to insert, as Backup fields; statuses to set, by id; ids to delete; and how many archives name no volume
  here."""
  inserts: list[dict] = field(default_factory=list)
  settles: dict[str, str] = field(default_factory=dict)
  deletes: list[str] = field(default_factory=list)
  unmatched: int = 0


def reconcile_catalog(identity: str, base_path: str, platform_keys: set[str], volume_keys: set[str],
                      rows: list[CatalogRow], volumes: dict[str, tuple[int, str]], started_at: datetime,
                      workers: dict[str, str]) -> CatalogPlan:
  """What makes the table mirror the store `identity` names, from its listing (full keys): rows of another store are
  dropped; of this store, a succeeded backup whose object is gone is dropped, and one left unfinished by an earlier run
  of the Manager (last written before `started_at`) is settled, succeeded when its object is there, else interrupted; listed
  keys no row records, deleted or not, are added, a volume archive only while its Volume (`volumes`, id to application
  id and name) is configured here, with its container's worker while that container is (`workers`, by container
  id)."""
  listed = platform_keys | volume_keys
  ours = [row for row in rows if row.storage_id == identity]
  recorded = {row.s3_path for row in ours}
  deletes = [row.id for row in rows if row.storage_id != identity or (
      row.status == "succeeded" and not row.deleted and row.s3_path not in listed)]
  settles = {row.id: "succeeded" if row.s3_path in listed else "interrupted"
             for row in ours if row.status in UNFINISHED and row.updated_at < started_at}
  inserts, unmatched = [], 0
  for key in sorted(platform_keys - recorded):
    if timestamp := timestamp_from_key(key):
      taken = datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT)
      inserts.append(dict(s3_path=key, storage_id=identity, type="platform", created_at=taken, slot_at=taken))
  for key in sorted(volume_keys - recorded):
    parsed = parse_volume_archive(key, base_path)
    if parsed is None:
      continue
    volume_id, container_id, timestamp = parsed
    if volume_id not in volumes:
      unmatched += 1
      continue
    application_id, volume_name = volumes[volume_id]
    taken = datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT)
    inserts.append(dict(s3_path=key, storage_id=identity, type="application", application=application_id,
                        source_volume_id=volume_id, source_volume_name=volume_name, container_id=container_id,
                        worker_hostname=workers.get(container_id), created_at=taken, slot_at=taken))
  return CatalogPlan(inserts, settles, deletes, unmatched)
