from services.applications.updates import application_changed
from services.db.models import Domain
from services.updates import Updates, UpdateSource


def domains_changed():
  Updates().changed(UpdateSource.DOMAINS)


def domain_changed(domain: Domain, previous_container=None):
  """A domain was written: the Domains page, and the pages of the applications and workers of its container and of
  the one it was linked to before."""
  domains_changed()
  for application in {container.application for container in (domain.container, previous_container) if container}:
    application_changed(application)
  for hostname in {container.worker_id for container in (domain.container, previous_container) if container}:
    Updates().changed(UpdateSource.WORKER_CONTAINERS, hostname)
