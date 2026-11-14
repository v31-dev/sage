import pytest

from services.db.models import Application, Container, Domain, Worker
from services.domains.commands import create_domain, delete_domain, patch_domain
from services.domains.config import allowed_origins, host
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
  def test_a_relink_moves_an_internal_name_to_the_new_containers_worker(self, red, blue):
    domain = shop(red)

    patch_domain(domain.id, {"container": blue.id})

    assert (stored(domain).container_id, stored(domain).namespace_worker_id) == (blue.id, "w2")
    assert host(stored(domain), "example.test") == "shop.blue.int.example.test"

  def test_a_later_patch_of_another_field_keeps_a_relink_a_stale_view_did_not_have(self, red, blue):
    domain = shop(red)
    stale = Domain.get_by_id(domain.id)
    patch_domain(domain.id, {"container": blue.id})

    patch_domain(stale.id, {"service": "shop"})

    assert (stored(domain).container_id, stored(domain).namespace_worker_id, stored(domain).service) == (blue.id, "w2", "shop")

  def test_a_patch_of_another_field_leaves_the_stored_namespace_as_it_is(self, red, blue):
    domain = shop(red)
    Domain.update(namespace_worker="w2").where(Domain.id == domain.id).execute()

    patch_domain(domain.id, {"port": 81})

    assert stored(domain).namespace_worker_id == "w2"

  def test_an_unlinked_internal_domain_keeps_its_namespace_and_can_still_be_edited(self, red):
    domain = shop(red)
    red.delete_instance()

    patch_domain(domain.id, {"name": "store", "port": 81})

    assert (stored(domain).container_id, stored(domain).namespace_worker_id, stored(domain).name) == (None, "w1", "store")

  def test_an_unlinked_internal_domain_switched_to_tcp_keeps_its_namespace(self, red):
    domain = shop(red)
    red.delete_instance()

    patch_domain(domain.id, {"type": "tcp"})

    assert (stored(domain).type, stored(domain).namespace_worker_id) == ("tcp", "w1")

  def test_an_unlinked_public_domain_cannot_become_internal(self, red):
    domain = shop(red, type="public")
    red.delete_instance()

    with pytest.raises(ValueError, match="An internal or TCP domain without a container is not allowed."):
      patch_domain(domain.id, {"type": "internal"})

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

  def test_a_relink_tells_both_applications_and_both_workers(self, red, blue, signals):
    domain = shop(red)
    signals.clear()

    patch_domain(domain.id, {"container": blue.id})

    assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                            (UpdateSource.APPLICATION, "demo/api"), (UpdateSource.WORKER_CONTAINERS, "w1"),
                            (UpdateSource.WORKER_CONTAINERS, "w2")}

  def test_a_patch_that_changes_nothing_says_nothing(self, red, signals):
    domain = shop(red, service="shop")
    signals.clear()

    patch_domain(domain.id, {"name": " shop ", "service": "shop", "container": red.id})

    assert signals == []

  @pytest.mark.parametrize("fields, error, message", [
      ({"port": 70000}, ValueError, "must be a number from 1 to 65535"),
      ({"container": "0123456789abcdef"}, LookupError, "Container not found."),
      ({"container": None}, LookupError, "Container not found."),
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


def test_deleting_an_unlinked_internal_domain_tells_its_namespace_workers_page(red, signals):
  domain = Domain.create(name="db", type="internal", port=5432, namespace_worker=red.worker)
  delete_domain(domain.id)
  assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.WORKER_CONTAINERS, "w1")}


def test_relinking_an_unlinked_internal_domain_tells_the_worker_it_leaves(red, blue, signals):
  domain = Domain.create(name="db", type="internal", port=5432, namespace_worker=red.worker)
  patch_domain(domain.id, {"container": blue.id})
  assert stored(domain).namespace_worker_id == "w2"
  assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/api"),
                          (UpdateSource.WORKER_CONTAINERS, "w1"), (UpdateSource.WORKER_CONTAINERS, "w2")}


def test_deleting_a_domain_tells_the_pages_it_was_on(red, signals):
  domain = shop(red)
  signals.clear()

  delete_domain(domain.id)

  assert not Domain.select().exists()
  assert set(signals) == {(UpdateSource.DOMAINS, None), (UpdateSource.APPLICATION, "demo/web"),
                          (UpdateSource.WORKER_CONTAINERS, "w1")}
