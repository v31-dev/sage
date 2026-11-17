"""The migration history: a log for each run in the data directory's `migrations/` folder (`migration-<run>.log`), a
JSON line each, kept whole. It lives outside the databases, since a refused run rolls its database back and an earlier
release reads the history of a later one it was reverted from."""
import json
import logging

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


def record(run: str, trigger: str, outcome: str, lines: list[dict], actions: list[dict] | None = None):
  """Appends to the run's log what it did to one file: the `lines` logged meanwhile (ts, level, message), then each
  manual action its steps left (`where`, the `command` or None, `why`) as a CRITICAL line, its command between `###`
  lines; `outcome` (migrated, recreated or refused) counts towards the run's. A log that can't be written is a warning,
  never the migration's failure."""
  now = utcnow().isoformat()
  entries = [*lines, *({"ts": now, "level": "CRITICAL", "action": True,
                        "message": f"{action['where']} - Run command to {action['why']}\n###\n{action['command']}\n###"
                        if action["command"] else f"{action['where']} - {action['why']}"} for action in actions or [])]
  MigrationRuns().saw(run, trigger, outcome, len(actions or []))
  folder = config.DATA_DIR / "migrations"
  try:
    folder.mkdir(exist_ok=True)
    path = folder / f"migration-{run}.log"
    header = [] if path.exists() else [{"run": run, "trigger": trigger, "release": config.VERSION, "at": now}]
    with open(path, "a") as log:
      log.write("".join(json.dumps(entry) + "\n" for entry in (*header, *entries)))
  except OSError as error:
    logger.warning(f"Could not write the migration log: {error}")


def migration_runs() -> list[dict]:
  """The runs' logs, newest first: each run's trigger, release, start, whether a file was refused, how many manual
  actions it left, and its `lines` as a log shows them, the actions last. A log without its first line, or a line that
  isn't whole (a write cut off), is skipped."""
  runs = []
  for path in (config.DATA_DIR / "migrations").glob("migration-*.log"):
    entries = []
    for line in path.read_text().splitlines():
      try:
        entries.append(json.loads(line))
      except ValueError:
        continue
    if not entries or not isinstance(entries[0], dict) or not {"run", "trigger", "release", "at"} <= entries[0].keys():
      continue
    lines = [entry for entry in entries[1:]
             if isinstance(entry, dict) and {"ts", "level", "message"} <= entry.keys()]
    actions = [line for line in lines if line.get("action")]
    runs.append({**{key: entries[0][key] for key in ("run", "trigger", "release", "at")},
                 "refused": any(line["level"] == "ERROR" for line in lines), "actions": len(actions),
                 "lines": [{key: line[key] for key in ("ts", "level", "message")}
                           for line in [*(line for line in lines if not line.get("action")), *actions]]})
  return sorted(runs, key=lambda run: run["at"], reverse=True)
