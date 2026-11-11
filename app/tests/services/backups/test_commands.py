import ast
import re
from pathlib import Path

import services.backups.commands as commands
from services.backups.commands import add_backup
from services.updates import UpdateSource

APP = Path(__file__).parents[3]
WRITE = re.compile(r"\bBackup\.(create|insert|insert_many|update|delete|replace|bulk_create)\(")


class TestAdd:
  def test_a_volumes_backup_is_recorded_with_its_store_volume_and_application(self, application, signals):
    backup = add_backup("app/k1", "store-a", "v1", "data", application.id)
    assert (backup.type, backup.s3_path, backup.storage_id, backup.source_volume_id, backup.source_volume_name,
            backup.application_id) == ("application", "app/k1", "store-a", "v1", "data", application.id)
    assert signals == [(UpdateSource.BACKUPS, None)]

  def test_the_platforms_backup_has_no_volume_or_application(self, db, signals):
    backup = add_backup("platform/k1", "store-a")
    assert (backup.type, backup.source_volume_id, backup.application_id) == ("platform", None, None)
    assert signals == [(UpdateSource.BACKUPS, None)]


def test_only_these_commands_write_backup_rows_and_each_says_so():
  writers = [path.relative_to(APP).as_posix() for folder in ("services", "utils", "values")
             for path in (APP / folder).rglob("*.py") if WRITE.search(path.read_text())]
  assert writers == ["services/backups/commands.py"]
  source = Path(commands.__file__).read_text()
  functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)]
  assert [node.name for node in functions if "UpdateSource.BACKUPS" not in ast.get_source_segment(source, node)] == []
