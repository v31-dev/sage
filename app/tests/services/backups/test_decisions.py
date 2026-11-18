from datetime import datetime, timedelta

import pytest

from services.backups.decisions import SlotRow, platform_restore_refusal, slot_due

SLOT, STARTED = datetime(2026, 1, 2, 3, 0), datetime(2026, 1, 2, 9, 0)
BUFFER = timedelta(minutes=5)


def row(status="succeeded", slot_at=SLOT, automatic=True, deleted=False, updated_at=STARTED + timedelta(minutes=1), id="b1"):
  return SlotRow(id, status, deleted, slot_at, updated_at, automatic)


def due(*rows):
  return slot_due(list(rows), SLOT, STARTED, BUFFER)


def test_a_slot_without_a_backup_is_due_with_none_to_take_again():
  assert due() == (True, None)


@pytest.mark.parametrize("held", [row("succeeded"), row("failed"), row("interrupted", deleted=True), row("queued"),
                                  row("running")])
def test_its_own_backup_that_succeeded_failed_was_deleted_or_is_being_taken_holds_it(held):
  assert due(held) == (False, None)


@pytest.mark.parametrize("left", [row("interrupted"), row("cancelled"), row("queued", updated_at=STARTED - timedelta(minutes=1)),
                                  row("running", updated_at=STARTED - timedelta(minutes=1))])
def test_its_own_backup_interrupted_cancelled_or_left_by_an_earlier_run_is_taken_again(left):
  assert due(left) == (True, "b1")


@pytest.mark.parametrize("offset, held", [(-5, True), (-4, True), (5, True), (-6, False), (6, False)])
def test_a_backup_not_of_a_schedule_holds_it_within_the_buffer(offset, held):
  assert due(row(slot_at=SLOT + timedelta(minutes=offset), automatic=False)) == (not held, None)


def test_another_slots_backup_of_a_schedule_never_holds_it():
  assert due(row(slot_at=SLOT - timedelta(minutes=1)), row(slot_at=SLOT + timedelta(minutes=1), id="b2")) == (True, None)


def test_a_backup_not_of_a_schedule_that_was_interrupted_neither_holds_it_nor_is_taken_again():
  assert due(row("interrupted", slot_at=SLOT - timedelta(minutes=2), automatic=False)) == (True, None)


@pytest.mark.parametrize("key, refusal", [
    ("base/backups/platform/20260102_030405_4444444444444444_0.3.0_4.db.enc", "A backup from a later version of Sage can't be restored."),
    ("base/backups/platform/20260102_030405_4444444444444444_0.2.0_3.db.enc", None),
    ("base/backups/platform/20260102_030405_4444444444444444_0.1.3_0.db.enc", None),
], ids=["a later schema", "this one", "an earlier one"])
def test_a_platform_backup_restores_unless_a_later_sage_took_it(key, refusal):
  assert platform_restore_refusal(key, 3) == refusal
