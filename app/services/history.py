from datetime import datetime

from services.db.models import Container, ContainerEvent, Worker, WorkerEvent
from services.logs import Logs
from utils.logging import sub_task_id, task_id
from values.names import qualified_name


def record_container_event(container: Container, action: str, transition: str, operation_id: str | None = None,
                           reason: dict | None = None, dedupe_key: str | None = None) -> bool:
  """Appends what happened to the container, with the running task and part; False when an event with `dedupe_key` is
  already recorded."""
  application = container.application
  return bool(ContainerEvent.insert(
      container=container.id, subject_id=container.id, action=action, transition=transition,
      operation_id=operation_id, reason=reason, task_id=task_id.get() or None, part_id=sub_task_id.get() or None,
      dedupe_key=dedupe_key, application_name=qualified_name(application.project_id, application.name),
      worker_hostname=container.worker_id,
  ).on_conflict_ignore().as_rowcount().execute())


def record_worker_event(hostname: str, action: str, transition: str, operation_id: str | None = None,
                        reason: dict | None = None, dedupe_key: str | None = None) -> bool:
  """Appends what happened to the worker, linked to its row when it has one; False when an event with `dedupe_key` is
  already recorded."""
  return bool(WorkerEvent.insert(
      worker=Worker.select(Worker.hostname).where(Worker.hostname == hostname).scalar(), subject_id=hostname,
      action=action, transition=transition, operation_id=operation_id, reason=reason,
      task_id=task_id.get() or None, part_id=sub_task_id.get() or None, dedupe_key=dedupe_key,
  ).on_conflict_ignore().as_rowcount().execute())


def task_parts(task_id: str) -> list[dict]:
  """The parts of a task that logged lines, in the order of their first line, each with the action and worker of its
  first container event (None for a part with none)."""
  events = {}
  for part, action, worker in (ContainerEvent.select(ContainerEvent.part_id, ContainerEvent.action, ContainerEvent.worker_hostname)
                               .where(ContainerEvent.task_id == task_id).order_by(ContainerEvent.id).tuples()):
    events.setdefault(part, {"type": action, "worker": worker})
  return [{"id": part, **events.get(part, {"type": None, "worker": None})} for part in Logs().query_task_parts(task_id)]


def purge_events(before: datetime) -> int:
  """Deletes the container and worker events recorded before `before`, and returns how many."""
  return sum(model.delete().where(model.created_at < before).execute() for model in (ContainerEvent, WorkerEvent))
