from services.base import Base

from .applications import ApplicationsMixin, application_schema
from .intake import IntakeMixin
from .requests import REQUESTS, RequestsMixin, requests_schema
from .store import StoreMixin
from .tasks import TASKS, TasksMixin, task_lines_schema


class Logs(StoreMixin, ApplicationsMixin, RequestsMixin, TasksMixin, IntakeMixin, Base):
  """The logs the manager keeps, each kind in its module: each application's lines (`applications`), Traefik's
  requests (`requests`) and the lines the manager logs inside its queued tasks (`tasks`), written as a worker's Vector
  pushes them (`intake`) or as a task logs them, in the files of the logs directory (`store`)."""

  # The files beside the applications' shards, each with its schema. An application's shard is named `project-app`,
  # so neither is one.
  FILES = {TASKS: task_lines_schema, REQUESTS: requests_schema}

  def _schema(self, name: str):
    return self.FILES.get(name, application_schema)
