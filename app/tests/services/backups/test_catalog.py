import itertools
from datetime import datetime

from services.backups.catalog import CatalogPlan, CatalogRow, reconcile_catalog
from services.backups.keys import volume_archive_key

CURRENT, OTHER = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
VOLUME, GONE_VOLUME, CONTAINER = "1111111111111111", "2222222222222222", "3333333333333333"
PLATFORM = "base/backups/platform/20260102_030405_4444444444444444.db.enc"
ARCHIVE = f"base/{volume_archive_key(VOLUME, CONTAINER, '20260103_040506_5555555555555555')}"
STRANGER = f"base/{volume_archive_key(GONE_VOLUME, CONTAINER, '20260104_050607_6666666666666666')}"
VOLUMES = {VOLUME: (7, "data")}


def plan(platform=(), volume=(), rows=()):
  return reconcile_catalog(CURRENT, "base", set(platform), set(volume), list(rows), VOLUMES)


def test_a_listed_platform_backup_is_added_from_its_time():
  assert plan(platform=[PLATFORM]) == CatalogPlan(
      [dict(s3_path=PLATFORM, storage_id=CURRENT, type="platform", created_at=datetime(2026, 1, 2, 3, 4, 5))])


def test_a_listed_volume_archive_is_added_to_its_volume_with_the_volumes_name():
  assert plan(volume=[ARCHIVE]).inserts == [dict(
      s3_path=ARCHIVE, storage_id=CURRENT, type="application", application=7, source_volume_id=VOLUME, source_volume_name="data",
      created_at=datetime(2026, 1, 3, 4, 5, 6))]


def test_an_archive_of_a_volume_not_configured_here_is_counted_and_not_added():
  assert plan(volume=[STRANGER]) == CatalogPlan(unmatched=1)


def test_keys_that_are_not_backups_are_skipped():
  assert plan(platform=["base/backups/platform/readme.txt"], volume=["base/backups/applications/demo/web/w1/data/x.tar.gz.enc"]) == (
      CatalogPlan())


def test_a_recorded_backup_still_listed_stays():
  assert plan(platform=[PLATFORM], rows=[CatalogRow("b1", PLATFORM, CURRENT)]) == CatalogPlan()


def test_a_recorded_backup_whose_object_is_gone_is_dropped():
  assert plan(rows=[CatalogRow("b1", PLATFORM, CURRENT)]) == CatalogPlan(deletes=["b1"])


def test_a_backup_of_another_store_is_dropped_even_where_the_same_key_is_listed_here():
  """Item 29: the same key in two stores is two backups; a row of the old store never stands for the new one's."""
  result = plan(platform=[PLATFORM], rows=[CatalogRow("b1", PLATFORM, OTHER)])
  assert result.deletes == ["b1"]
  assert [row["storage_id"] for row in result.inserts] == [CURRENT]


def test_applying_any_plan_leaves_exactly_the_listed_backups_of_this_store():
  listings = [PLATFORM, ARCHIVE, STRANGER]
  rows = [CatalogRow("b1", PLATFORM, CURRENT), CatalogRow("b2", ARCHIVE, OTHER), CatalogRow("b3", STRANGER, CURRENT)]
  for listed, recorded in itertools.product(itertools.product((False, True), repeat=3), itertools.product((False, True), repeat=3)):
    keys = {key for key, present in zip(listings, listed) if present}
    held = [row for row, present in zip(rows, recorded) if present]
    result = reconcile_catalog(CURRENT, "base", keys & {PLATFORM}, keys - {PLATFORM}, held, VOLUMES)

    after = {(row.s3_path, row.storage_id) for row in held if row.id not in result.deletes}
    after |= {(row["s3_path"], row["storage_id"]) for row in result.inserts}
    expected = {(key, CURRENT) for key in keys if key != STRANGER}
    expected |= {(row.s3_path, CURRENT) for row in held if row.s3_path == STRANGER and row.storage_id == CURRENT and STRANGER in keys}
    assert after == expected, (keys, held)
