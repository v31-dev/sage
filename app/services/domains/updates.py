from services.applications.updates import application_changed
from services.db.models import Container, Domain, DomainCaller
from services.updates import Updates, UpdateSource


def domains_changed():
  Updates().changed(UpdateSource.DOMAINS)


def domain_changed(domain: Domain):
  """A domain was written: the Domains page, and the pages of its container's application and worker."""
  domains_changed()
  application_changed(domain.container.application)
  Updates().changed(UpdateSource.WORKER_CONTAINERS, domain.container.worker_id)


def callers_observed(hostname: str):
  """A worker's applications' networks were seen to change: the pages of each Domain that allows an application with a
  container there."""
  for domain in (Domain.select().join(DomainCaller).join(Container, on=(Container.application == DomainCaller.application))
                 .where(Container.worker == hostname).distinct()):
    domain_changed(domain)
