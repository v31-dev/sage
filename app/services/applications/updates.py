from services.db.models import Application, Container
from services.updates import Updates, UpdateSource


def application_changed(application: Application):
  """The application, or a container, domain or volume of it, was written: the page of that application is sent again."""
  Updates().changed(UpdateSource.APPLICATION, f"{application.project.name}/{application.name}")


def container_changed(container: Container):
  """A container was written: its application's page, its worker's page and the Domains page show it."""
  application_changed(container.application)
  Updates().changed(UpdateSource.WORKER_CONTAINERS, container.worker_id)
  Updates().changed(UpdateSource.DOMAINS)


def hosts_changed(application: Application):
  """What the pages of the workers show of an application (its label, its project's) was written, besides the
  application itself."""
  application_changed(application)
  hostnames = {container.worker_id for container in application.containers}
  for hostname in hostnames:
    Updates().changed(UpdateSource.WORKER_CONTAINERS, hostname)
  if hostnames:
    Updates().changed(UpdateSource.DOMAINS)
