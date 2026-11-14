"""The Backup table as a mirror of the configured store: what to add and what to drop after listing it."""
from dataclasses import dataclass, field
from datetime import datetime

from services.backups.keys import BACKUP_TIMESTAMP_FORMAT, parse_volume_archive, timestamp_from_key


@dataclass(frozen=True)
class CatalogRow:
  id: str
  s3_path: str
  storage_id: str


@dataclass(frozen=True)
class CatalogPlan:
  """Rows to insert, as Backup fields; ids to delete; and how many archives name no volume here."""
  inserts: list[dict] = field(default_factory=list)
  deletes: list[str] = field(default_factory=list)
  unmatched: int = 0


def reconcile_catalog(identity: str, base_path: str, platform_keys: set[str], volume_keys: set[str],
                      rows: list[CatalogRow], volumes: dict[str, tuple[int, str]]) -> CatalogPlan:
  """What makes the table mirror the store `identity` names, from its listing (full keys): rows of another store, and
  of this store whose object is gone, are dropped; listed keys not recorded are added, a volume archive only while its
  Volume (`volumes`, id to application id and name) is configured here."""
  recorded = {row.s3_path for row in rows if row.storage_id == identity}
  deletes = [row.id for row in rows if row.storage_id != identity or row.s3_path not in platform_keys | volume_keys]
  inserts, unmatched = [], 0
  for key in sorted(platform_keys - recorded):
    if timestamp := timestamp_from_key(key):
      inserts.append(dict(s3_path=key, storage_id=identity, type="platform",
                          created_at=datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT)))
  for key in sorted(volume_keys - recorded):
    parsed = parse_volume_archive(key, base_path)
    if parsed is None:
      continue
    volume_id, _, timestamp = parsed
    if volume_id not in volumes:
      unmatched += 1
      continue
    application_id, volume_name = volumes[volume_id]
    inserts.append(dict(s3_path=key, storage_id=identity, type="application", application=application_id,
                        source_volume_id=volume_id, source_volume_name=volume_name,
                        created_at=datetime.strptime(timestamp, BACKUP_TIMESTAMP_FORMAT)))
  return CatalogPlan(inserts, deletes, unmatched)
