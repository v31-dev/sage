import ast
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import services.backups.commands as commands
from services.backups.commands import add_backups, cancel_queued_backups, settle_backups
from services.db.models import Backup, Volume
from services.updates import UpdateSource

APP = Path(__file__).parents[3]
WRITE = re.compile(r"\bBackup\.(create|insert|insert_many|update|delete|replace|bulk_create)\(")


SLOT = datetime(2026, 1, 2, 3, 4)
C1, C2 = SimpleNamespace(id="c1", worker_id="w1"), SimpleNamespace(id="c2", worker_id="w2")


class TestAdd:
  def test_a_slots_volume_backups_are_recorded_queued_with_their_store_volume_application_and_container(
          self, application, signals):
    volumes = [Volume.create(application=application, name=name, path=f"/{name}") for name in ("data", "logs")]
    ids = add_backups("store-a", SLOT, True, ["app/k1", "app/k2"], volumes, C1)
    assert [(backup.type, backup.s3_path, backup.storage_id, backup.source_volume_id, backup.source_volume_name,
             backup.application_id, backup.container_id, backup.worker_hostname, backup.slot_at, backup.automatic, backup.status)
            for backup in map(Backup.get_by_id, ids)] == [
        ("application", f"app/k{index + 1}", "store-a", volume.id, volume.name, application.id, "c1", "w1", SLOT, True, "queued")
        for index, volume in enumerate(volumes)]
    assert signals[0] == (UpdateSource.BACKUPS, None) and sorted(signals[1:]) == sorted(
        (UpdateSource.VOLUME_BACKUPS, volume.id) for volume in volumes)

  def test_the_platforms_backup_has_no_volume_application_or_container(self, db, signals):
    [backup_id] = add_backups("store-a", SLOT, False, ["platform/k1"])
    backup = Backup.get_by_id(backup_id)
    assert (backup.type, backup.source_volume_id, backup.application_id, backup.container_id, backup.status) == (
        "platform", None, None, None, "queued")


class TestSettle:
  def test_only_a_backup_still_queued_or_running_is_settled(self, db, signals):
    ids = add_backups("store-a", SLOT, False, ["k1", "k2", "k3"])
    settle_backups(ids[:1], "running")
    settle_backups(ids[1:2], "succeeded")
    signals.clear()
    assert settle_backups(ids, "failed", "AccessDenied")
    assert [(row.status, row.reason) for row in map(Backup.get_by_id, ids)] == [
        ("failed", "AccessDenied"), ("succeeded", None), ("failed", "AccessDenied")]
    assert not settle_backups(ids, "cancelled") and signals == [(UpdateSource.BACKUPS, None)]

  def test_a_replaced_or_cancelled_request_cancels_only_its_kinds_queued_backups(self, application, db):
    volume = Volume.create(application=application, name="data", path="/data")
    manual, kept, running = (add_backups("s", SLOT, False, [key], [volume], C1)[0] for key in ("k1", "k2", "k3"))
    settle_backups([running], "running")
    automatic = add_backups("s", SLOT, True, ["k4"], [volume], C1)[0]
    elsewhere = add_backups("s", SLOT, False, ["k5"], [volume], C2)[0]
    platform, newer = add_backups("s", SLOT, False, ["p1"])[0], add_backups("s", SLOT, False, ["p2"])[0]
    assert cancel_queued_backups([kept], "c1")
    assert cancel_queued_backups([newer])
    assert [Backup.get_by_id(backup_id).status for backup_id in (manual, kept, running, automatic, elsewhere, platform, newer)] == [
        "cancelled", "queued", "running", "queued", "queued", "cancelled", "queued"]


def test_only_these_commands_write_backup_rows_and_each_says_so():
  writers = [path.relative_to(APP).as_posix() for folder in ("services", "utils", "values")
             for path in (APP / folder).rglob("*.py") if WRITE.search(path.read_text())]
  assert writers == ["services/backups/commands.py"]
  source = Path(commands.__file__).read_text()
  functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)]
  assert [node.name for node in functions if "backups_changed(" not in ast.get_source_segment(source, node)] == []
