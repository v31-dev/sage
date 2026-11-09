import re

import pytest

from services.manager import _common


class TestContentHash:
  def test_is_sixteen_hex_characters(self):
    assert re.fullmatch(r"[0-9a-f]{16}", _common.content_hash({"a": 1}))

  def test_does_not_depend_on_key_order(self):
    assert _common.content_hash({"a": 1, "b": [1, 2]}) == _common.content_hash({"b": [1, 2], "a": 1})

  def test_depends_on_the_content_and_on_list_order(self):
    payloads = [{"a": 1}, {"a": 2}, {"a": [1, 2]}, {"a": [2, 1]}]
    assert len({_common.content_hash(payload) for payload in payloads}) == 4

  def test_known_value(self):
    # Worker revision stamps are compared against this: a change of format makes every worker re-sync.
    assert _common.content_hash({"a": 1}) == "f9d86028c6e0d64e"


class TestTemplatesDigest:
  @pytest.fixture
  def templates(self, tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "app_dir", tmp_path)
    folder = tmp_path / "templates" / "worker"
    folder.mkdir(parents=True)
    (folder / "a.yml").write_text("a: 1\n")
    (folder / "b.yml").write_text("b: 2\n")
    return folder

  def test_is_sixteen_hex_characters(self, templates):
    assert re.fullmatch(r"[0-9a-f]{16}", _common.templates_digest("worker/a.yml"))

  def test_does_not_depend_on_the_order_of_the_paths(self, templates):
    assert _common.templates_digest("worker/a.yml", "worker/b.yml") == _common.templates_digest("worker/b.yml", "worker/a.yml")

  def test_changes_when_a_file_changes(self, templates):
    before = _common.templates_digest("worker/a.yml", "worker/b.yml")
    (templates / "b.yml").write_text("b: 3\n")
    assert _common.templates_digest("worker/a.yml", "worker/b.yml") != before

  def test_changes_when_a_file_is_renamed(self, templates):
    (templates / "c.yml").write_text("a: 1\n")
    assert _common.templates_digest("worker/a.yml") != _common.templates_digest("worker/c.yml")

  def test_a_missing_file_raises(self, templates):
    with pytest.raises(FileNotFoundError):
      _common.templates_digest("worker/missing.yml")
