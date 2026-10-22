import config


def test_data_dir_override_moves_the_paths(data_dir):
  assert config.DATA_DIR == data_dir
  assert config.DB_PATH.parent == data_dir


def test_database_opens_in_the_data_dir_and_creates_the_tables(db, data_dir):
  assert (data_dir / "data.db").exists()
  assert {"project", "application", "container", "worker", "task"} <= set(db.get_tables())
