"""The migration history, `migrations.log` in the data directory: a JSON line for each SQLite file a migration run
changed or refused, kept whole. It lives outside the databases, since a refused run rolls its database back and an
earlier release reads the history of a later one it was reverted from."""
import json
import logging
from pathlib import Path

import config
from services.base import Base
from utils.common import new_id, utcnow

logger = logging.getLogger(__name__)

# This boot's run; a platform restore's install is a run of its own.
BOOT = new_id()
# A file's outcomes, the least to the most a run's overall outcome is: refused when a file was, else recreated when one
# was, else migrated.
OUTCOMES = ("migrated", "recreated", "refused")


class MigrationRuns(Base):
  """The runs this process recorded, as `GET /api` shows the latest of them: its id, trigger, release, overall outcome
  and how many manual actions it left. Memory only: the next start has nothing new to show."""

  def __init__(self):
    super().__init__()
    self.runs: dict[str, dict] = {}

  def saw(self, run: str, trigger: str, outcome: str, actions: int):
    with self.lock:
      seen = self.runs.get(run, {"run": run, "trigger": trigger, "release": config.VERSION, "outcome": OUTCOMES[0],
                                 "actions": 0})
      self.runs[run] = {**seen, "outcome": max(seen["outcome"], outcome, key=OUTCOMES.index),
                        "actions": seen["actions"] + actions}

  def latest(self) -> dict | None:
    """The run this process started last; None when it recorded none."""
    with self.lock:
      return next(reversed(self.runs.values()), None)


def record(run: str, trigger: str, path: str, before: int | None, after: int | None, outcome: str, detail: str | None,
           actions: list[dict] | None = None):
  """Appends what a run did to the file at `path`: `outcome` is migrated, recreated or refused, from version `before`
  towards `after` (None for a file that wasn't a sound database), with the manual `actions` its steps left. A history
  that can't be written is a warning, never the migration's failure."""
  try:
    name = str(Path(path).relative_to(config.DATA_DIR))
  except ValueError:
    name = str(path)
  entry = {"run": run, "trigger": trigger, "at": utcnow().isoformat(), "release": config.VERSION, "file": name,
           "from": before, "to": after, "outcome": outcome, "detail": detail, "actions": actions or []}
  MigrationRuns().saw(run, trigger, outcome, len(entry["actions"]))
  try:
    with open(config.DATA_DIR / "migrations.log", "a") as log:
      log.write(json.dumps(entry) + "\n")
  except OSError as error:
    logger.warning(f"Could not write the migration history: {error}")


def migration_runs() -> list[dict]:
  """The runs in the history, newest first: each with its trigger, release, start, overall outcome (`OUTCOMES`) and its
  files in the order they ran. A line that isn't a whole
  entry (a write cut off) is skipped."""
  runs: dict[str, dict] = {}
  try:
    lines = (config.DATA_DIR / "migrations.log").read_text().splitlines()
  except FileNotFoundError:
    return []
  for line in lines:
    try:
      entry = json.loads(line)
      run = runs.setdefault(entry["run"], {"run": entry["run"], "trigger": entry["trigger"], "release": entry["release"],
                                           "at": entry["at"], "files": []})
      run["files"].append({**{key: entry[key] for key in ("file", "from", "to", "outcome", "detail", "at")},
                           "actions": entry.get("actions", [])})
    except (ValueError, KeyError, TypeError):
      continue
  for run in runs.values():
    run["outcome"] = max((file["outcome"] for file in run["files"]), key=OUTCOMES.index)
  return sorted(runs.values(), key=lambda run: run["at"], reverse=True)
