"""How a request finds an application, or one of its volumes or containers, from the names in its path: a LookupError
when there is none."""
from services.db.models import Application, Container, Project, Volume


def find_application(project: str, name: str) -> Application:
  """The application named so in the project, with its project."""
  application = (Application.select(Application, Project).join(Project)
                 .where((Project.name == project) & (Application.name == name)).first())
  if application is None:
    raise LookupError("Application not found.")
  return application


def find_container(application_id: int, container_id: str) -> Container:
  """The application's container with that id."""
  container = Container.get_or_none((Container.id == container_id) & (Container.application == application_id))
  if container is None:
    raise LookupError("Container not found.")
  return container


def find_volume(application_id: int, name: str) -> Volume:
  """The application's volume of that name."""
  volume = Volume.get_or_none((Volume.application == application_id) & (Volume.name == name))
  if volume is None:
    raise LookupError("Volume not found.")
  return volume
