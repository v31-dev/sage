"""The task lines: what the manager logs inside its queued tasks, in `logs/tasks.db`, read by task. A handler on the
root logger hands them to a drain thread, which writes them."""
import logging
import queue
import threading
from datetime import datetime, timezone
from functools import partial

from peewee import CharField, Model, SqliteDatabase, TextField

from services.updates import UpdateFeed, Updates
from utils.logging import task_id, task_lines

from .store import Store, stored_time

logger = logging.getLogger(__name__)

TASKS = "tasks"


def task_lines_schema(db: SqliteDatabase) -> Store:
  class TaskLogs(Model):
    task_id = CharField()
    ts = CharField()
    level = CharField()
    logger = CharField()
    message = TextField()

    class Meta:
      database = db
      indexes = ((("task_id",), False), (("ts",), False))

  db.create_tables([TaskLogs], safe=True)
  return Store(db, TaskLogs)


class TasksMixin:
  def __init__(self):
    super().__init__()
    # The handler enqueues the lines; the drain thread writes them off the calling thread (often the event loop).
    self._captured = queue.SimpleQueue()

  def start_capture(self):
    """Write the lines of queued tasks from now on: the handler on the root logger, and the drain thread."""
    threading.Thread(target=self._capture_loop, name="sage-log-capture", daemon=True).start()
    logging.getLogger().addHandler(_CaptureHandler(self._captured))

  def _capture_loop(self):
    while True:
      entry = self._captured.get()
      try:
        self.write_task_line(entry)
      except Exception:
        # This thread runs outside any task, so its own line goes to stdout only.
        logger.warning(f"Could not write a line of task {entry['task_id']}.", exc_info=True)

  def write_task_line(self, entry: dict):
    """A task's line, to the task logs and to the task's feed."""
    row = {"id": self._store(TASKS).lines.insert(**entry).execute(), **entry}
    self._check_sizes(TASKS)
    Updates().publish(UpdateFeed.TASK_LOGS, [row], key=entry["task_id"])

  def query_task_logs(self, task_id: str, after_id: int = 0) -> dict:
    """All of a task's lines, in the order they were written (after the line `after_id`, if given)."""
    lines = self._store(TASKS).lines
    query = lines.select().where(lines.task_id == task_id)
    if after_id:
      query = query.where(lines.id > after_id)
    return {"lines": list(query.order_by(lines.id).dicts())}


class _CaptureHandler(logging.Handler):
  """Hands the lines of a queued task to the drain thread, so the calling thread never blocks on a write."""

  def __init__(self, captured: queue.SimpleQueue):
    super().__init__()
    self._captured = captured
    self.setFormatter(logging.Formatter("%(message)s"))

  def emit(self, record):
    try:
      lines = task_lines.get()
      if lines is not None:
        lines.write(partial(self._captured.put, {
            "task_id": task_id.get(),
            "ts": stored_time(datetime.fromtimestamp(record.created, timezone.utc)),
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }))
    except Exception:
      self.handleError(record)
