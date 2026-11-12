from services.applications.updates import application_changed
from services.db.models import Domain
from services.updates import Updates, UpdateSource


def domains_changed():
  Updates().changed(UpdateSource.DOMAINS)


def domain_changed(domain: Domain, previous_container=None, previous_namespace: str | None = None):
  """A domain was written: the Domains page, the pages of the applications and workers of its container and of the
  one it was linked to before, and those of its namespace worker now and before, whose removal its internal domains
  refuse."""
  domains_changed()
  for application in {container.application for container in (domain.container, previous_container) if container}:
    application_changed(application)
  for hostname in ({container.worker_id for container in (domain.container, previous_container) if container}
                   | {domain.namespace_worker_id, previous_namespace}) - {None}:
    Updates().changed(UpdateSource.WORKER_CONTAINERS, hostname)
