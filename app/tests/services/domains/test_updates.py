from services.db.models import Container, Domain
from services.domains.updates import domain_changed
from services.updates import UpdateSource


def test_a_domain_tells_the_domains_page_and_its_containers_application_and_worker(signals, application, worker):
  domain = Domain.create(name="db", type="internal", port=5432, container=Container.create(application=application, worker=worker),
                         namespace_worker=worker)

  domain_changed(domain)

  assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                          (UpdateSource.WORKER_CONTAINERS, "w1")}
