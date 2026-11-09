from services.db.models import Application, Container, Domain, Worker
from services.domains.updates import domain_changed
from services.updates import UpdateSource


def test_a_relinked_domain_tells_both_applications_and_both_workers(signals, application, worker):
  before = Container.create(application=application, worker=worker)
  other = Container.create(application=Application.create(project=application.project, name="api", label="API"),
                           worker=Worker.create(hostname="w2"))
  domain = Domain.create(name="shop", type="public", port=80, container=other)

  domain_changed(domain, before)

  assert set(signals) == {
      (UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"), (UpdateSource.APPLICATION, "demo/api"),
      (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.WORKER_CONTAINERS, "w2")}


def test_an_unlinked_domain_tells_only_the_domains_page(signals, db):
  domain_changed(Domain.create(name="shop", type="public", port=80))
  assert signals == [(UpdateSource.DOMAINS, None)]
