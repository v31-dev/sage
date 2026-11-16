"""The backups' decisions: whether a schedule's slot is due."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from services.backups.catalog import UNFINISHED


@dataclass(frozen=True)
class SlotRow:
  id: str
  status: str
  deleted: bool
  slot_at: datetime
  updated_at: datetime
  automatic: bool


def slot_due(rows: list[SlotRow], slot: datetime, started_at: datetime, buffer: timedelta) -> tuple[bool, str | None]:
  """Whether a schedule's `slot` is due for one container's volume, or for the platform, from its backups' rows in the
  configured store; and the slot's own row to take again, if any. The slot's own row, and any other row not of a
  schedule within `buffer` of it, holds it while it succeeded or failed, was deleted, or is being taken by this run
  of the Manager (written since `started_at`); one interrupted, cancelled or left unfinished by an earlier run does
  not, and the slot's own such row is taken again. Another slot's row never holds it, so a schedule more frequent than
  the buffer keeps every slot."""
  near = [row for row in rows if row.slot_at == slot or (not row.automatic and abs(row.slot_at - slot) <= buffer)]
  if any(row.deleted or row.status in ("succeeded", "failed") or (row.status in UNFINISHED and row.updated_at >= started_at)
         for row in near):
    return False, None
  return True, next((row.id for row in near if row.automatic and row.slot_at == slot), None)
