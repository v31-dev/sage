import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from services.manager import _common


def domain(name="web", type="public", port=80):
  return SimpleNamespace(name=name, type=type, port=port)


def container(hostname="w1", ip="100.64.0.1", online=True, status="active", tag=None):
  return SimpleNamespace(domain_tag=tag, status=status, worker=SimpleNamespace(hostname=hostname, ip=ip, online=online))


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


class TestTimestampFromKey:
  @pytest.mark.parametrize("key", [
      "backups/platform/20260102_030405.db.enc",
      "/backups/platform/20260102_030405.db.enc/",
      "bucket/backups/applications/project/app/worker/data/20260102_030405.tar.gz.enc",
      "20260102_030405",
  ])
  def test_finds_the_timestamp_in_the_file_name(self, key):
    assert _common.timestamp_from_key(key) == "20260102_030405"

  @pytest.mark.parametrize("key", [
      "backups/platform/notes.txt",
      "backups/platform/2026010_030405.db.enc",
      "backups/platform/20260102-030405.db.enc",
      "",
  ])
  def test_no_timestamp_gives_none(self, key):
    assert _common.timestamp_from_key(key) is None

  def test_only_the_last_path_component_counts(self):
    assert _common.timestamp_from_key("backups/20260102_030405/readme.txt") is None


class TestIsExpired:
  cutoff = datetime(2026, 1, 10, 0, 0, 0)

  @pytest.mark.parametrize("timestamp, expired", [
      ("20260109_235959", True),
      ("20260110_000000", False),
      ("20260110_000001", False),
      ("20250101_000000", True),
  ])
  def test_timestamp_before_the_cutoff_is_expired(self, timestamp, expired):
    assert _common.is_expired_timestamp(timestamp, self.cutoff) is expired

  @pytest.mark.parametrize("timestamp", ["", "garbage", "20261340_000000", "20260102"])
  def test_unparseable_timestamp_is_not_expired(self, timestamp):
    assert _common.is_expired_timestamp(timestamp, self.cutoff) is False

  @pytest.mark.parametrize("key, expired", [
      ("backups/platform/20260101_000000.db.enc", True),
      ("backups/platform/20260201_000000.db.enc", False),
      ("backups/platform/readme.txt", False),
  ])
  def test_backup_key_is_judged_by_its_timestamp(self, key, expired):
    assert _common.is_expired_backup_key(key, self.cutoff) is expired


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

  def test_the_shipped_template_sets_are_digested(self):
    assert re.fullmatch(r"[0-9a-f]{16}", _common.WORKER_INFRA_TEMPLATES)
    assert re.fullmatch(r"[0-9a-f]{16}", _common.APPLICATION_ROUTING_TEMPLATES)
    assert _common.WORKER_INFRA_TEMPLATES != _common.APPLICATION_ROUTING_TEMPLATES


class TestRoutingInputHash:
  def hash(self, domains=None, containers=None, domain_name="example.test"):
    return _common.routing_input_hash(domain_name, [domain()] if domains is None else domains, [container()] if containers is None else containers)

  def test_does_not_depend_on_the_order_of_domains_or_containers(self):
    domains = [domain("a"), domain("b", "internal", 8080)]
    containers = [container("w1", "100.64.0.1", tag="blue"), container("w2", "100.64.0.2", tag="green")]
    assert self.hash(domains, containers) == self.hash(domains[::-1], containers[::-1])

  @pytest.mark.parametrize("changed", [
      [domain(name="other")],
      [domain(type="internal")],
      [domain(port=8080)],
      [domain(), domain("second")],
      [],
  ])
  def test_changes_with_the_domains(self, changed):
    assert self.hash(changed) != self.hash()

  def test_changes_with_the_cloudflare_domain(self):
    assert self.hash(domain_name="other.test") != self.hash()

  def test_changes_with_the_address_of_an_active_container(self):
    assert self.hash(containers=[container(ip="100.64.0.9")]) != self.hash()

  def test_changes_with_the_worker_an_active_container_runs_on(self):
    assert self.hash(containers=[container(hostname="w2")]) != self.hash()

  def test_changes_with_another_active_container(self):
    assert self.hash(containers=[container(), container("w2", "100.64.0.2")]) != self.hash()

  def test_a_container_that_is_not_active_is_left_out(self):
    assert self.hash(containers=[container(), container("w2", status="error")]) == self.hash()
    assert self.hash(containers=[container(status="error")]) == self.hash(containers=[container(status="inactive")])

  def test_a_container_on_an_offline_worker_is_left_out(self):
    assert self.hash(containers=[container(online=False)]) == self.hash(containers=[container(status="inactive")])

  def test_a_domain_tag_counts_even_when_its_container_is_not_active(self):
    assert self.hash(containers=[container(status="inactive", tag="blue")]) != self.hash(containers=[container(status="inactive")])

  def test_a_new_tag_changes_the_hash(self):
    assert self.hash(containers=[container(tag="blue")]) != self.hash()

  def test_editing_a_routing_template_changes_the_hash(self, monkeypatch):
    before = self.hash()
    monkeypatch.setattr(_common, "APPLICATION_ROUTING_TEMPLATES", "edited")
    assert self.hash() != before
