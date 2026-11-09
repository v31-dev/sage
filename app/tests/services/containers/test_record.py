import json
import subprocess
from dataclasses import replace
from datetime import datetime

import pytest

from services.containers.placement import placement_dir, records_command
from services.containers.record import (Applied, Authority, Baseline, Operation, Recovery, Settled, TargetRead, TargetRecord, decode_record,
                                        encode_record, parse_record_listing)

AT = datetime(2026, 1, 2, 3, 4, 5, 678901)
HASH = "a" * 64
AUTHORITY = Authority("0123456789abcdef", "1111111111111111", "2222222222222222")
FULL = TargetRecord(
    AUTHORITY,
    Applied("d1", HASH, AT),
    Settled("d1", "running", "succeeded", AT, None),
    Operation("b1", "backup_preserve", "recovering", AT, 930, None, None,
              Baseline("d1", "running", "d1", ("dockerid1", "dockerid2")), "interrupted", "manager restarted",
              Recovery("unresolved", "old work still settling"), "task1"),
)


def mutated(change) -> str:
  value = json.loads(encode_record(FULL))
  change(value)
  return json.dumps(value)


def test_a_record_survives_its_encoding_on_one_line_with_its_format_version():
  text = encode_record(FULL)
  assert "\n" not in text
  assert json.loads(text)["format_version"] == 1
  assert decode_record(text) == FULL


@pytest.mark.parametrize("record", [
    TargetRecord(AUTHORITY),
    TargetRecord(AUTHORITY, operation=Operation("d2", "deploy", "executing", AT, 960, input_hash=HASH, task_ref="t1")),
    TargetRecord(AUTHORITY, Applied("d2", HASH, AT), Settled("d2", "running", "succeeded", AT),
                 Operation("d2", "deploy", "settled", AT, 0, AT, HASH, outcome="succeeded")),
    TargetRecord(AUTHORITY, settled=Settled("d3", "stopped", "failed", AT, "pull: timeout"),
                 operation=Operation("d3", "deploy", "recovering", AT, 960, input_hash=HASH, outcome="failed", reason="pull: timeout",
                                     recovery=Recovery("verified"))),
    TargetRecord(AUTHORITY, operation=Operation("s1", "stop", "settled", AT, 0, AT, outcome="succeeded")),
], ids=["new", "a deploy running", "a deploy committed", "a failed deploy recovered", "a stop"])
def test_each_shape_of_record_survives_its_encoding(record):
  assert decode_record(encode_record(record)) == record


def drop(*path):
  def change(value):
    for key in path[:-1]:
      value = value[key]
    del value[path[-1]]
  return change


def put(value_at, *path):
  def change(value):
    for key in path[:-1]:
      value = value[key]
    value[path[-1]] = value_at
  return change


@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    mutated(put(1, "extra")),
    mutated(drop("settled")),
    mutated(put(2, "format_version")),
    mutated(put(True, "format_version")),
    mutated(put("container1", "authority", "container_id")),
    mutated(put("abc", "applied", "config_hash")),
    mutated(put("paused", "settled", "runtime_goal")),
    mutated(put("2026-01-01T12:00:00+00:00", "applied", "committed_at")),
    mutated(put("yesterday", "settled", "committed_at")),
    mutated(put(-1, "operation", "effect_bound_seconds")),
    mutated(put(True, "operation", "effect_bound_seconds")),
    mutated(put("5", "operation", "effect_bound_seconds")),
    mutated(put("settled", "operation", "phase")),
    mutated(put(None, "operation", "outcome")),
    mutated(put("succeeded", "operation", "outcome")),
    mutated(put("deploy", "operation", "contract")),
    mutated(put(None, "operation", "baseline")),
    mutated(put(HASH, "operation", "input_hash")),
    mutated(put("d1", "operation", "baseline", "docker_ids")),
    mutated(put("x", "operation", "baseline", "extra")),
    mutated(put("abandoned", "operation", "recovery", "result")),
    mutated(put("", "operation", "id")),
], ids=["not JSON", "not an object", "an unknown field", "a missing field", "another format version", "a boolean version",
        "an authority that is not an id", "a fingerprint that is not one", "an unknown goal", "an aware time", "not a time",
        "a negative bound", "a boolean bound", "a bound in text", "settled without a finish", "recovering without an outcome",
        "recovering after success", "a deploy without a fingerprint", "an automatic backup without a baseline",
        "a fingerprint outside a deploy", "docker ids that are not a list", "an unknown baseline field", "an unknown recovery",
        "an empty id"])
def test_anything_else_is_refused(text):
  with pytest.raises(ValueError):
    decode_record(text)


def test_a_settled_operation_needs_its_finish_and_an_executing_one_has_none():
  record = TargetRecord(AUTHORITY, operation=Operation("s1", "stop", "settled", AT, 0, AT, outcome="succeeded"))
  with pytest.raises(ValueError, match="Only a settled operation"):
    decode_record(encode_record(TargetRecord(AUTHORITY, operation=Operation("s1", "stop", "executing", AT, 0, AT, outcome="succeeded"))))
  value = json.loads(encode_record(record))
  value["operation"]["finished_at"] = None
  with pytest.raises(ValueError, match="Only a settled operation"):
    decode_record(json.dumps(value))


def listing_line(container_id: str, content: str) -> str:
  return f"/opt/sage/containers/{container_id}/record.json:{content}"


def test_a_listing_reads_each_record_by_its_container_and_refuses_what_is_not_one_of_its_own():
  of = {container_id: TargetRecord(replace(AUTHORITY, container_id=container_id))
        for container_id in ("3333333333333333", "5555555555555555")}
  lines = [
      listing_line("2222222222222222", encode_record(FULL)),
      listing_line("3333333333333333", "{broken"),
      listing_line("4444444444444444", encode_record(of["3333333333333333"])),
      listing_line("5555555555555555", encode_record(of["5555555555555555"])),
      listing_line("5555555555555555", encode_record(of["5555555555555555"])),
      "/opt/sage/containers/not-an-id/record.json:{}",
      "grep: /opt/sage/containers/*/record.json: No such file or directory",
  ]

  assert parse_record_listing(lines) == {
      "2222222222222222": TargetRead("present", FULL),
      "3333333333333333": TargetRead("invalid"),
      "4444444444444444": TargetRead("invalid"),
      "5555555555555555": TargetRead("invalid"),
  }


def test_the_listing_command_reads_the_records_of_a_real_placements_directory(tmp_path):
  home = str(tmp_path)
  for record in (FULL, TargetRecord(Authority("0123456789abcdef", "1111111111111111", "3333333333333333"))):
    directory = tmp_path / placement_dir(home, record.authority.container_id).removeprefix(home + "/")
    directory.mkdir(parents=True)
    (directory / "record.json").write_text(encode_record(record))
  (tmp_path / "containers" / "4444444444444444").mkdir()

  output = subprocess.run(["sh", "-c", records_command(home)], capture_output=True, text=True, check=True).stdout

  reads = parse_record_listing(output.splitlines())
  assert reads.keys() == {"2222222222222222", "3333333333333333"}
  assert reads["2222222222222222"] == TargetRead("present", FULL)


def test_the_listing_command_lists_nothing_when_there_are_no_placements(tmp_path):
  result = subprocess.run(["sh", "-c", records_command(str(tmp_path))], capture_output=True, text=True)
  assert (result.returncode, result.stdout) == (0, "")
