import pytest

from services.db.models import Container, Domain
from services.domains.config import DomainConfig
from services.domains.decisions import ApplyOutcome, RouteObservation
from services.domains.state import Domains, domain_state
from services.updates import UpdateSource


@pytest.fixture
def domain(application, worker):
  return Domain.create(name="shop", type="public", port=8080, container=Container.create(application=application, worker=worker))


@pytest.fixture
def snapshot(domain):
  return DomainConfig.capture(domain, "example.test")


class TestDomains:
  def test_nothing_is_known_of_a_domain_until_it_is_applied_or_scanned(self, domain, snapshot):
    assert (Domains().outcome(domain.id), Domains().route(domain.id, "w1"), Domains().dns_conforms(domain.id, snapshot.content_hash)) == (
        None, RouteObservation("unobserved"), None)

  def test_a_route_seen_on_another_worker_says_nothing_of_this_one(self, domain):
    Domains().record_route(domain, "w2", RouteObservation("present", "f1"))
    assert Domains().route(domain.id, "w1") == RouteObservation("unobserved")
    assert Domains().route(domain.id, "w2") == RouteObservation("present", "f1")

  def test_dns_compared_with_another_fingerprint_says_nothing_of_this_one(self, domain):
    Domains().record_dns(domain, "f1", True)
    assert (Domains().dns_conforms(domain.id, "f2"), Domains().dns_conforms(domain.id, "f1")) == (None, True)

  def test_a_change_shows_the_domain_again_and_a_repeat_does_not(self, domain, signals):
    for _ in range(2):
      Domains().record_outcome(domain, ApplyOutcome("f1", True))
      Domains().record_route(domain, "w1", RouteObservation("absent"))
      Domains().record_dns(domain, "f1", True)
    assert signals.count((UpdateSource.DOMAINS, None)) == 3
    assert (UpdateSource.APPLICATION, "demo/web") in signals and (UpdateSource.WORKER_CONTAINERS, "w1") in signals

  def test_forgetting_a_domain_drops_all_of_it_and_no_other(self, domain, application, worker):
    other = Domain.create(name="api", type="public", port=80, container=domain.container)
    for each in (domain, other):
      Domains().record_outcome(each, ApplyOutcome("f1", True))
      Domains().record_route(each, "w1", RouteObservation("absent"))
      Domains().record_dns(each, "f1", True)
    Domains().forget(domain.id)
    assert (Domains().outcome(domain.id), Domains().route(domain.id, "w1"), Domains().dns_conforms(domain.id, "f1")) == (
        None, RouteObservation("unobserved"), None)
    assert (Domains().outcome(other.id), Domains().route(other.id, "w1"), Domains().dns_conforms(other.id, "f1")) == (
        ApplyOutcome("f1", True), RouteObservation("absent"), True)


class TestDomainState:
  def test_after_a_restart_it_is_pending_until_applied_or_scanned(self, snapshot):
    state = domain_state(snapshot, True, True, False)
    assert (state.view.status, state.view.reason) == ("pending", "not_applied")

  def test_the_held_facts_of_its_fingerprint_and_worker_decide_it(self, domain, snapshot):
    Domains().record_route(domain, "w1", RouteObservation("present", snapshot.content_hash))
    Domains().record_dns(domain, snapshot.content_hash, True)
    state = domain_state(snapshot, True, True, False)
    assert (state.view.status, state.view.conforms) == ("synced", True)
    assert (state.facts.route, state.facts.dns_conforms, state.facts.linked) == (
        RouteObservation("present", snapshot.content_hash), True, True)

  def test_an_outcome_of_an_older_fingerprint_does_not_count(self, domain, snapshot):
    Domains().record_outcome(domain, ApplyOutcome("older", False, "dns", "timeout"))
    assert domain_state(snapshot, True, True, False).view.status == "pending"
    Domains().record_outcome(domain, ApplyOutcome(snapshot.content_hash, False, "dns", "timeout"))
    view = domain_state(snapshot, True, True, False).view
    assert (view.status, view.reason, view.error) == ("error", "dns", "timeout")

  def test_a_queued_or_running_apply_shows_it_syncing(self, snapshot):
    assert domain_state(snapshot, True, True, True).view.status == "syncing"

  def test_its_worker_not_ready_is_why_it_is_pending(self, snapshot):
    assert domain_state(snapshot, True, False, False).view.reason == "worker_not_ready"

  def test_while_its_container_is_not_known_it_is_pending_for_that(self, snapshot):
    assert domain_state(snapshot, None, True, False).view.reason == "eligibility_unknown"
