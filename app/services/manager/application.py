import logging
from contextlib import contextmanager

from services.applications.updates import application_changed, container_changed
from services.db import Application, Container, Event
from utils.logging import new_task_id, sub_task_id, task_id

logger = logging.getLogger(__name__)


def save_application(application: Application):
  application.save()
  application_changed(application)


@contextmanager
def container_part(container: Container, kind: str):
  """One container's part of the running task (its deploy, stop, delete, backup or restore): records its `Event`, and
  the lines logged inside carry the part's id, which it yields."""
  part = new_task_id()
  Event.create(container=container, type=kind, application_task_id=task_id.get(), container_task_id=part)
  token = sub_task_id.set(part)
  try:
    yield part
  finally:
    sub_task_id.reset(token)


class ApplicationMixin:
  def save_container(self, container: Container):
    was_active = Container.select(Container.status).where(Container.id == container.id).scalar() == "active"
    container.save()
    container_changed(container)
    if was_active != (container.status == "active"):
      self.request_discovery_sync()
