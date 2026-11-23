import pytest

from services.db.models import Application, Container, Domain, DomainCaller, Worker
from services.domains.commands import create_domain, delete_domain, patch_domain
from services.domains.config import allowed_origins
from services.updates import UpdateSource
from utils.errors import Conflict


@pytest.fixture
def red(application, worker):
  Worker.update(subdomain="red").where(Worker.hostname == worker.hostname).execute()
  return Container.create(application=application, worker=worker)


@pytest.fixture
def blue(project):
  other = Application.create(project=project, name="api", label="API", image="nginx")
  return Container.create(application=other, worker=Worker.create(hostname="w2", subdomain="blue"))


def stored(domain):
  return Domain.get_by_id(domain.id)


def shop(container, **fields):
  return create_domain(container.id, {"name": "shop", "type": "internal", "port": 80, **fields})


class TestCreate:
  @pytest.mark.parametrize("type, namespace", [("public", None), ("internal", "w1"), ("tcp", "w1")])
  def test_an_internal_or_tcp_name_is_in_its_containers_workers_namespace_and_a_public_one_in_none(self, red, type, namespace):
    assert stored(shop(red, type=type)).namespace_worker_id == namespace

  def test_it_tells_the_domains_page_and_its_containers_application_and_worker(self, red, signals):
    shop(red)
    assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                            (UpdateSource.WORKER_CONTAINERS, "w1")}

  def test_text_is_trimmed_blank_service_and_pool_are_none_and_origins_are_normalized(self, red):
    domain = shop(red, name=" shop ", service=" ", pool="", cors_origins=[" https://app.example.test/ ", "https://app.example.test"])
    assert (stored(domain).name, stored(domain).service, stored(domain).pool, stored(domain).cors_origins) == (
        "shop", None, None, ["https://app.example.test"])

  def test_tcp_keeps_no_origins(self, red):
    assert stored(shop(red, type="tcp", cors_origins=["https://app.example.test"])).cors_origins is None

  @pytest.mark.parametrize("name", ["core", "int", "discovery"])
  def test_sages_own_public_names_are_reserved_and_free_inside(self, red, name):
    with pytest.raises(ValueError, match="reserved for Sage's own addresses"):
      shop(red, name=name, type="public")
    shop(red, name=name)

  @pytest.mark.parametrize("first, second", [("internal", "internal"), ("internal", "tcp"), ("tcp", "internal")])
  def test_an_internal_or_tcp_name_is_taken_once_on_a_worker(self, red, first, second):
    shop(red, type=first)
    with pytest.raises(Conflict, match="'shop' is already an internal or TCP domain on worker w1."):
      shop(red, type=second, port=81)

  def test_a_public_name_is_taken_once_whatever_its_container(self, red, blue):
    shop(red, type="public")
    with pytest.raises(Conflict, match="'shop' is already a public domain."):
      shop(blue, type="public")

  def test_the_same_name_can_be_internal_on_two_workers_and_public(self, red, blue):
    shop(red)
    shop(blue, type="tcp")
    shop(red, type="public")
    assert Domain.select().where(Domain.name == "shop").count() == 3

  @pytest.mark.parametrize("container, fields, error, message", [
      (None, {"name": "shop", "port": 80}, ValueError, "A domain without a container is not allowed."),
      ("0123456789abcdef", {"name": "shop", "port": 80}, LookupError, "Container not found."),
      ("red", {"port": 80}, ValueError, "name is required."),
      ("red", {"name": "shop"}, ValueError, "must be a number from 1 to 65535"),
      ("red", {"name": "shop", "port": 0}, ValueError, "must be a number from 1 to 65535"),
      ("red", {"name": "Shop", "port": 80}, ValueError, "lowercase letters, digits and dashes"),
      ("red", {"name": "shop", "port": 80, "type": "bogus"}, ValueError, "Invalid choice 'bogus'"),
      ("red", {"name": "shop", "port": 80, "cors_origins": ["ftp://x"]}, ValueError, "Invalid origin"),
      ("red", {"name": "shop", "port": 80, "worker": "w2"}, ValueError, "Unknown fields: worker."),
  ])
  def test_an_invalid_domain_is_refused_and_not_stored(self, red, signals, container, fields, error, message):
    with pytest.raises(error, match=message):
      create_domain(red.id if container == "red" else container, fields)
    assert not Domain.select().exists()
    assert signals == []


class TestPatch:

  def test_a_patch_of_another_field_leaves_the_stored_namespace_as_it_is(self, red, blue):
    domain = shop(red)
    Domain.update(namespace_worker="w2").where(Domain.id == domain.id).execute()

    patch_domain(domain.id, {"port": 81})

    assert stored(domain).namespace_worker_id == "w2"

  def test_a_domain_saved_again_is_not_refused_by_itself(self, red):
    domain = shop(red, type="tcp")
    patch_domain(domain.id, {"name": "shop", "port": 81})
    assert stored(domain).port == 81

  def test_a_rename_onto_a_name_taken_in_its_namespace_is_refused(self, red, blue):
    shop(red)
    other = shop(red, name="store")
    shop(blue, name="cart")

    with pytest.raises(Conflict, match="'shop' is already an internal or TCP domain on worker w1."):
      patch_domain(other.id, {"name": "shop"})
    patch_domain(other.id, {"name": "cart"})

  def test_switching_to_tcp_drops_the_origins_and_origins_sent_to_tcp_are_dropped(self, red):
    domain = shop(red, cors_origins=["https://app.example.test"])
    patch_domain(domain.id, {"type": "tcp"})
    assert stored(domain).cors_origins is None
    patch_domain(domain.id, {"cors_origins": ["https://app.example.test"]})
    assert stored(domain).cors_origins is None

  def test_resetting_explicit_origins_restores_its_own_address(self, red):
    domain = shop(red, type="public", cors_origins=["https://frontend.example.test"])
    patch_domain(domain.id, {"cors_origins": []})
    assert stored(domain).cors_origins is None
    assert allowed_origins(stored(domain), "example.test") == ("https://shop.example.test",)

  def test_explicit_origins_of_one_domain_are_not_rewritten_when_another_is_renamed(self, red):
    frontend = shop(red, name="app", type="public")
    api = shop(red, name="api", type="public", cors_origins=["https://app.example.test"])

    patch_domain(frontend.id, {"name": "frontend"})

    assert stored(api).cors_origins == ["https://app.example.test"]

  def test_a_patch_that_changes_nothing_says_nothing(self, red, signals):
    domain = shop(red, service="shop")
    signals.clear()

    patch_domain(domain.id, {"name": " shop ", "service": "shop"})

    assert signals == []

  @pytest.mark.parametrize("fields, error, message", [
      ({"port": 70000}, ValueError, "must be a number from 1 to 65535"),
      ({"container": "0123456789abcdef"}, ValueError, "Unknown fields: container."),
      ({"name": None}, ValueError, "name is required."),
      ({"namespace_worker": "w2"}, ValueError, "Unknown fields: namespace_worker."),
  ])
  def test_an_invalid_patch_is_refused_and_nothing_of_it_is_written(self, red, signals, fields, error, message):
    domain = shop(red)
    signals.clear()

    with pytest.raises(error, match=message):
      patch_domain(domain.id, {"service": "other", **fields})

    assert (stored(domain).service, stored(domain).port, stored(domain).container_id) == (None, 80, red.id)
    assert signals == []

  def test_a_missing_domain_is_not_found(self, db):
    with pytest.raises(LookupError, match="Domain not found."):
      patch_domain("0123456789abcdef", {"port": 81})


class TestCallers:
  def callers(self, domain):
    return sorted(caller.application_id for caller in DomainCaller.select().where(DomainCaller.domain == domain.id))

  @pytest.mark.parametrize("type", ["internal", "tcp"])
  def test_an_internal_or_tcp_domain_keeps_the_applications_allowed_to_call_it_once_each(self, red, blue, type):
    domain = shop(red, type=type, callers=[blue.application_id, red.application_id, blue.application_id])
    assert self.callers(domain) == sorted([red.application_id, blue.application_id])

  @pytest.mark.parametrize("callers, message", [
      ("2", "callers must be a list of application ids."), ([True], "callers must be a list of application ids."),
      (["2"], "callers must be a list of application ids."), ([999], "Unknown applications: 999.")])
  def test_callers_that_are_not_applications_are_refused_and_nothing_is_stored(self, red, callers, message):
    with pytest.raises(ValueError, match=message):
      shop(red, callers=callers)
    assert not Domain.select().exists() and not DomainCaller.select().exists()

  def test_a_public_domain_takes_none(self, red, blue):
    with pytest.raises(ValueError, match="A public domain takes no allowed applications."):
      shop(red, type="public", callers=[blue.application_id])
    assert not Domain.select().exists()

  def test_a_patch_replaces_them_and_one_without_them_keeps_them(self, red, blue, signals):
    domain = shop(red, callers=[red.application_id])
    signals.clear()
    patch_domain(domain.id, {"callers": [blue.application_id]})
    assert self.callers(domain) == [blue.application_id] and signals
    patch_domain(domain.id, {"port": 81})
    assert self.callers(domain) == [blue.application_id]

  def test_a_patch_that_sends_the_same_ones_says_nothing(self, red, blue, signals):
    domain = shop(red, callers=[red.application_id, blue.application_id])
    signals.clear()
    patch_domain(domain.id, {"callers": [blue.application_id, red.application_id]})
    assert signals == []

  def test_a_domain_made_public_loses_them(self, red, blue):
    domain = shop(red, callers=[blue.application_id])
    patch_domain(domain.id, {"type": "public"})
    assert self.callers(domain) == []

  def test_they_go_with_their_domain_and_with_their_application(self, red, project):
    other = Application.create(project=project, name="cron", label="Cron", image="busybox")
    kept, gone = shop(red, callers=[red.application_id, other.id]), shop(red, name="api", callers=[red.application_id])
    Application.delete().where(Application.id == other.id).execute()
    delete_domain(gone.id)
    assert [(caller.domain_id, caller.application_id) for caller in DomainCaller.select()] == [(kept.id, red.application_id)]


def test_deleting_a_domain_tells_the_pages_it_was_on(red, signals):
  domain = shop(red)
  signals.clear()

  delete_domain(domain.id)

  assert not Domain.select().exists()
  assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                          (UpdateSource.WORKER_CONTAINERS, "w1")}
