from services.applications.updates import application_changed
from services.db.models import Domain
from services.updates import Updates, UpdateSource


def domains_changed():
  Updates().changed(UpdateSource.DOMAINS)


def domain_changed(domain: Domain):
  """A domain was written: the Domains page, and the pages of its container's application and worker."""
  domains_changed()
  application_changed(domain.container.application)
  Updates().changed(UpdateSource.WORKER_CONTAINERS, domain.container.worker_id)
