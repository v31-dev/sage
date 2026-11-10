"""Where a container lives on its worker and how its runtime is named and labelled, all by its opaque id."""
import re

from services.containers.record import Authority, TargetRead, parse_record_listing
from utils.common import OPAQUE_ID

LABEL_INSTANCE = "sage.instance"
LABEL_GENERATION = "sage.generation"
LABEL_CONTAINER = "sage.container"
LABEL_DEPLOYMENT = "sage.deployment"
LABEL_APPLICATION = "sage.application"
LABELS = (LABEL_INSTANCE, LABEL_GENERATION, LABEL_CONTAINER, LABEL_DEPLOYMENT, LABEL_APPLICATION)


def placements_dir(home: str) -> str:
  return f"{home}/containers"


def placement_dir(home: str, container_id: str) -> str:
  return f"{placements_dir(home)}/{container_id}"


def record_path(home: str, container_id: str) -> str:
  return f"{placement_dir(home, container_id)}/record.json"


def volume_dir(home: str, container_id: str, volume_id: str) -> str:
  return f"{placement_dir(home, container_id)}/volumes/{volume_id}"


def records_command(home: str, container_id: str = "*") -> str:
  """Lists the container's record, or every record on the worker, for `record.parse_record_listing`."""
  return f"grep -H . {placements_dir(home)}/{container_id}/record.json 2>/dev/null || true"


# Between the directories and the records in a listing of placements.
PLACEMENTS_END = "--- records"


def placements_command(home: str) -> str:
  """Lists the worker's placement directories, then every record, for `parse_placements`."""
  return f"ls -1 {placements_dir(home)} 2>/dev/null; echo '{PLACEMENTS_END}'; {records_command(home)}"


def parse_placements(lines: list[str]) -> tuple[set[str], dict[str, TargetRead]]:
  """The container ids of the placement directories and the records `placements_command` listed."""
  if PLACEMENTS_END not in lines:
    raise ValueError("Not a listing of placements.")
  end = lines.index(PLACEMENTS_END)
  return {name for name in lines[:end] if re.fullmatch(OPAQUE_ID, name)}, parse_record_listing(lines[end + 1:])


def runtime_name(container_id: str) -> str:
  """The Compose project and the Docker container of the placement."""
  return f"sage-{container_id}"


def runtime_owner(name: str) -> str | None:
  """The container id a runtime name belongs to."""
  match = re.fullmatch(rf"sage-({OPAQUE_ID})", name)
  return match[1] if match else None


def runtime_labels(authority: Authority, deployment_id: str, application: str) -> dict[str, str]:
  return {LABEL_INSTANCE: authority.instance_id, LABEL_GENERATION: authority.state_generation,
          LABEL_CONTAINER: authority.container_id, LABEL_DEPLOYMENT: deployment_id, LABEL_APPLICATION: application}


def labels_authority(labels: dict[str, str]) -> Authority | None:
  """The authority a runtime's labels name; None unless all three are there."""
  values = [labels.get(label) for label in (LABEL_INSTANCE, LABEL_GENERATION, LABEL_CONTAINER)]
  return Authority(*values) if all(values) else None
