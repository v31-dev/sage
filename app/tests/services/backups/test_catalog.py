import itertools
from datetime import datetime, timedelta

from services.backups.catalog import CatalogPlan, CatalogRow, reconcile_catalog
from services.backups.keys import volume_archive_key

CURRENT, OTHER = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
VOLUME, GONE_VOLUME, CONTAINER = "1111111111111111", "2222222222222222", "3333333333333333"
PLATFORM = "base/backups/platform/20260102_030405_4444444444444444_0.2.0_1.db.enc"
ARCHIVE = f"base/{volume_archive_key(VOLUME, CONTAINER, '20260103_040506_5555555555555555')}"
STRANGER = f"base/{volume_archive_key(GONE_VOLUME, CONTAINER, '20260104_050607_6666666666666666')}"
VOLUMES = {VOLUME: (7, "data")}
WORKERS = {CONTAINER: "w1"}
STARTED = datetime(2026, 1, 10)
BEFORE, AFTER = STARTED - timedelta(minutes=1), STARTED + timedelta(minutes=1)


def plan(platform=(), volume=(), rows=()):
  return reconcile_catalog(CURRENT, "base", set(platform), set(volume), list(rows), VOLUMES, STARTED, WORKERS)


def stored(backup_id, key, store=CURRENT, status="succeeded", deleted=False, updated_at=BEFORE):
  return CatalogRow(backup_id, key, store, status, deleted, updated_at)


def test_a_listed_platform_backup_is_added_from_its_time():
  taken = datetime(2026, 1, 2, 3, 4, 5)
  assert plan(platform=[PLATFORM]) == CatalogPlan(
      [dict(s3_path=PLATFORM, storage_id=CURRENT, type="platform", created_at=taken, slot_at=taken)])


def test_a_listed_volume_archive_is_added_to_its_volume_and_container_with_the_volumes_name():
  taken = datetime(2026, 1, 3, 4, 5, 6)
  assert plan(volume=[ARCHIVE]).inserts == [dict(
      s3_path=ARCHIVE, storage_id=CURRENT, type="application", application=7, source_volume_id=VOLUME, source_volume_name="data",
      container_id=CONTAINER, worker_hostname="w1", created_at=taken, slot_at=taken)]


def test_an_archive_of_a_volume_not_configured_here_is_counted_and_not_added():
  assert plan(volume=[STRANGER]) == CatalogPlan(unmatched=1)


def test_keys_that_are_not_backups_are_skipped():
  assert plan(platform=["base/backups/platform/readme.txt"], volume=["base/backups/applications/demo/web/w1/data/x.tar.gz.enc"]) == (
      CatalogPlan())


def test_a_recorded_backup_still_listed_stays():
  assert plan(platform=[PLATFORM], rows=[stored("b1", PLATFORM)]) == CatalogPlan()


def test_a_recorded_backup_whose_object_is_gone_is_dropped():
  assert plan(rows=[stored("b1", PLATFORM)]) == CatalogPlan(deletes=["b1"])


def test_a_backup_that_was_not_completed_stays_without_its_object():
  rows = [stored(f"b{index}", PLATFORM, status=status) for index, status in enumerate(("failed", "cancelled", "interrupted"))]
  assert plan(rows=rows) == CatalogPlan()


def test_a_deleted_backup_stays_and_its_leftover_object_is_not_added_again():
  assert plan(platform=[PLATFORM], rows=[stored("b1", PLATFORM, deleted=True)]) == CatalogPlan()
  assert plan(rows=[stored("b1", PLATFORM, deleted=True)]) == CatalogPlan()


def test_a_backup_an_earlier_run_left_unfinished_is_settled_by_its_object():
  for status in ("queued", "running"):
    assert plan(platform=[PLATFORM], rows=[stored("b1", PLATFORM, status=status)]) == CatalogPlan(settles={"b1": "succeeded"})
    assert plan(rows=[stored("b1", PLATFORM, status=status)]) == CatalogPlan(settles={"b1": "interrupted"})


def test_a_backup_this_run_is_taking_is_left_alone():
  for status in ("queued", "running"):
    assert plan(rows=[stored("b1", PLATFORM, status=status, updated_at=AFTER)]) == CatalogPlan()
    assert plan(platform=[PLATFORM], rows=[stored("b1", PLATFORM, status=status, updated_at=AFTER)]) == CatalogPlan()


def test_a_backup_of_another_store_is_dropped_even_where_the_same_key_is_listed_here():
  """Item 29: the same key in two stores is two backups; a row of the old store never stands for the new one's."""
  result = plan(platform=[PLATFORM], rows=[stored("b1", PLATFORM, OTHER)])
  assert result.deletes == ["b1"]
  assert [row["storage_id"] for row in result.inserts] == [CURRENT]


def test_a_backup_of_another_store_is_dropped_whatever_its_status():
  rows = [stored(status, PLATFORM, OTHER, status=status, deleted=status == "failed")
          for status in ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")]
  assert sorted(plan(rows=rows).deletes) == sorted(row.id for row in rows)


def test_applying_any_plan_records_each_listed_backup_once_and_keeps_every_record_of_one_not_completed():
  listings = [PLATFORM, ARCHIVE, STRANGER]
  states = [None, ("succeeded", False), ("succeeded", True), ("failed", False), ("running", False)]
  for listed, held_states, store in itertools.product(
          itertools.product((False, True), repeat=3), itertools.product(states, repeat=3), (CURRENT, OTHER)):
    keys = {key for key, present in zip(listings, listed) if present}
    held = [stored(f"b{index}", key, CURRENT if index else store, *state)
            for index, (key, state) in enumerate(zip(listings, held_states)) if state is not None]
    result = reconcile_catalog(CURRENT, "base", keys & {PLATFORM}, keys - {PLATFORM}, held, VOLUMES, STARTED, WORKERS)

    after = [(row.s3_path, result.settles.get(row.id, row.status), row.deleted) for row in held
             if row.storage_id == CURRENT and row.id not in result.deletes]
    after += [(row["s3_path"], "succeeded", False) for row in result.inserts]
    recorded = [key for key, _, _ in after]
    assert sorted(recorded) == sorted(set(recorded)), (keys, held)
    assert keys - {STRANGER} <= set(recorded), (keys, held)
    assert all(key in keys for key, status, deleted in after if status == "succeeded" and not deleted), (keys, held)
    assert not any(status in ("queued", "running") for _, status, _ in after), (keys, held)
    assert all(row.id not in result.deletes for row in held
               if row.storage_id == CURRENT and (row.status != "succeeded" or row.deleted)), (keys, held)
    assert all(row.id in result.deletes for row in held if row.storage_id != CURRENT), (keys, held)
