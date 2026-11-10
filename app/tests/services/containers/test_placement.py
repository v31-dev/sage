import subprocess
from pathlib import Path

import pytest

from services.containers.placement import (LABEL_APPLICATION, LABEL_CONTAINER, LABEL_DEPLOYMENT, LABEL_GENERATION, LABEL_INSTANCE, LABELS,
                                           labels_authority, parse_placements, placement_dir, placements_command, record_path, runtime_labels,
                                           runtime_name, runtime_owner, volume_dir)
from services.containers.record import Authority, TargetRead
from utils.common import new_id

AUTHORITY = Authority("0123456789abcdef", "1111111111111111", "2222222222222222")


def test_a_placement_and_its_record_and_volumes_are_under_its_container_id():
  assert placement_dir("/opt/sage", "2222222222222222") == "/opt/sage/containers/2222222222222222"
  assert record_path("/opt/sage", "2222222222222222") == "/opt/sage/containers/2222222222222222/record.json"
  assert volume_dir("/opt/sage", "2222222222222222", "3333333333333333") == (
      "/opt/sage/containers/2222222222222222/volumes/3333333333333333")


def test_a_runtime_is_named_by_its_container_id_and_the_name_gives_it_back():
  container_id = new_id()
  assert runtime_owner(runtime_name(container_id)) == container_id


@pytest.mark.parametrize("name", ["traefik", "2222222222222222", "sage-2222222222222222x", "sage-2222222222222222-1",
                                  "SAGE-2222222222222222", "sage-22222222222222", "demo-web"])
def test_any_other_runtime_name_is_not_a_placement(name):
  assert runtime_owner(name) is None


def test_the_labels_name_its_authority_deployment_and_application():
  labels = runtime_labels(AUTHORITY, "d1", "demo-web")
  assert set(labels) == set(LABELS)
  assert (labels[LABEL_DEPLOYMENT], labels[LABEL_APPLICATION]) == ("d1", "demo-web")
  assert labels_authority(labels) == AUTHORITY


@pytest.mark.parametrize("missing", [LABEL_INSTANCE, LABEL_GENERATION, LABEL_CONTAINER])
def test_labels_without_all_of_the_authority_name_none(missing):
  labels = runtime_labels(AUTHORITY, "d1", "demo-web")
  del labels[missing]
  assert labels_authority(labels) is None


def test_a_listing_of_placements_is_their_directories_and_records(tmp_path):
  home = str(tmp_path)
  for name in ("0123456789abcdef", "fedcba9876543210", "not-an-id"):
    Path(placement_dir(home, name)).mkdir(parents=True)
  Path(record_path(home, "0123456789abcdef")).write_text("not a record\n")
  output = subprocess.run(["sh", "-c", placements_command(home)], capture_output=True, text=True, check=True).stdout
  directories, records = parse_placements(output.splitlines())
  assert (directories, records) == ({"0123456789abcdef", "fedcba9876543210"}, {"0123456789abcdef": TargetRead("invalid")})


def test_a_worker_without_placements_lists_none(tmp_path):
  output = subprocess.run(["sh", "-c", placements_command(str(tmp_path / "home"))], capture_output=True, text=True, check=True).stdout
  assert parse_placements(output.splitlines()) == (set(), {})


def test_an_answer_that_is_not_a_listing_is_refused():
  with pytest.raises(ValueError):
    parse_placements(["0123456789abcdef"])
