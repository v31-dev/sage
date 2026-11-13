from datetime import datetime

import pytest

from services.backups.keys import (archive_name, archive_scope, archive_write_scope, is_expired_backup_key, is_expired_timestamp,
                                   parse_volume_archive, store_scope, taken_at, timestamp_from_key, volume_archive_key)
from utils.common import new_id
from utils.queue import _scope_conflict

VOLUME, CONTAINER, ARCHIVE = "1111111111111111", "2222222222222222", "4444444444444444"
NAME = f"20260102_030405_{ARCHIVE}"


class TestArchiveName:
  def test_two_taken_in_the_same_second_differ_and_each_parses_to_that_second(self):
    names = {archive_name(datetime(2026, 1, 2, 3, 4, 5)) for _ in range(2)}
    assert len(names) == 2
    assert {timestamp_from_key(f"backups/platform/{name}.db.enc") for name in names} == {"20260102_030405"}


class TestTimestampFromKey:
  @pytest.mark.parametrize("key", [
      f"backups/platform/{NAME}.db.enc",
      f"/backups/platform/{NAME}.db.enc/",
      f"base/backups/applications/{VOLUME}/{CONTAINER}/{NAME}.tar.gz.enc",
  ])
  def test_finds_the_timestamp_in_an_archives_name(self, key):
    assert timestamp_from_key(key) == "20260102_030405"

  @pytest.mark.parametrize("key", [
      "backups/platform/notes.txt",
      "backups/platform/20260102_030405.db.enc",
      f"backups/platform/2026010_030405_{ARCHIVE}.db.enc",
      f"backups/platform/20260102-030405_{ARCHIVE}.db.enc",
      "backups/platform/20260102_030405_ABCDEF0123456789.db.enc",
      f"backups/platform/{NAME}.db",
      NAME,
      "",
  ], ids=["no timestamp", "no archive id", "short date", "dash", "not an id", "not encrypted", "no suffix", "empty"])
  def test_any_other_name_gives_none(self, key):
    assert timestamp_from_key(key) is None

  def test_only_the_last_path_component_counts(self):
    assert timestamp_from_key(f"backups/{NAME}.db.enc/readme.txt") is None


class TestTakenAt:
  def test_it_is_the_time_in_the_key_as_people_read_it_and_unknown_without_one(self):
    assert (taken_at(f"base/backups/platform/{NAME}.db.enc"), taken_at("base/backups/platform/latest.db.enc")) == (
        "2026-01-02 03:04 UTC", "an unknown time")


class TestIsExpired:
  cutoff = datetime(2026, 1, 10, 0, 0, 0)

  @pytest.mark.parametrize("timestamp, expired", [
      ("20260109_235959", True),
      ("20260110_000000", False),
      ("20260110_000001", False),
      ("20250101_000000", True),
  ])
  def test_timestamp_before_the_cutoff_is_expired(self, timestamp, expired):
    assert is_expired_timestamp(timestamp, self.cutoff) is expired

  @pytest.mark.parametrize("timestamp", ["", "garbage", "20261340_000000", "20260102"])
  def test_unparseable_timestamp_is_not_expired(self, timestamp):
    assert is_expired_timestamp(timestamp, self.cutoff) is False

  @pytest.mark.parametrize("key, expired", [
      (f"backups/platform/20260101_000000_{ARCHIVE}.db.enc", True),
      (f"backups/platform/20260201_000000_{ARCHIVE}.db.enc", False),
      ("backups/platform/20260101_000000.db.enc", False),
      ("backups/platform/readme.txt", False),
  ])
  def test_backup_key_is_judged_by_its_timestamp(self, key, expired):
    assert is_expired_backup_key(key, self.cutoff) is expired


class TestVolumeArchiveKey:
  def test_it_is_named_by_its_volume_placement_and_name_and_parses_back(self):
    volume, container = new_id(), new_id()
    key = volume_archive_key(volume, container, NAME)
    assert key == f"backups/applications/{volume}/{container}/{NAME}.tar.gz.enc"
    assert parse_volume_archive(f"base/{key}", "/base/") == (volume, container, "20260102_030405")

  @pytest.mark.parametrize("key", [
      f"other/backups/applications/{VOLUME}/{CONTAINER}/{NAME}.tar.gz.enc",
      f"base/backups/platform/{VOLUME}/{CONTAINER}/{NAME}.tar.gz.enc",
      f"base/backups/applications/demo/web/w1/data/{NAME}.tar.gz.enc",
      f"base/backups/applications/{VOLUME}/{NAME}.tar.gz.enc",
      f"base/backups/applications/{VOLUME}/{CONTAINER}/{NAME}.tar.gz",
      f"base/backups/applications/{VOLUME}/{CONTAINER}/notes.tar.gz.enc",
      f"base/backups/applications/{VOLUME}/{CONTAINER}/20260102_030405.tar.gz.enc",
      f"base/backups/applications/ABCDEF0123456789/{CONTAINER}/{NAME}.tar.gz.enc",
  ], ids=["another base path", "the platform prefix", "a key by names", "no placement", "not encrypted", "no timestamp",
          "no archive id", "not an id"])
  def test_any_other_key_is_not_a_volume_archive(self, key):
    assert parse_volume_archive(key, "base") is None


def test_a_store_covers_its_archives_writes_and_each_is_apart_from_the_others_and_another_stores():
  parts = [archive_write_scope("sa", new_id()), archive_write_scope("sa", "platform"), archive_scope("sa", "base/backups/platform/a.db.enc"),
           archive_scope("sa", "base/backups/platform/b.db.enc")]
  assert all(_scope_conflict(store_scope("sa"), part) for part in parts)
  assert not any(_scope_conflict(one, other) for index, one in enumerate(parts) for other in parts[index + 1:])
  assert not any(_scope_conflict(store_scope("sb"), part) for part in parts)
  assert archive_scope("sa", "base/backups/platform/a.db.enc") == archive_scope("sa", "base/backups/platform/a.db.enc")
