from dataclasses import FrozenInstanceError

import pytest
import yaml

from services.db.models import Container, Domain, Worker
from services.domains.config import (ROUTING_DIGEST, DomainConfig, address, allowed_origins, domain_artifact_name, domain_artifact_owner, host,
                                     parse_route_listing, validate_port)
from utils.common import new_id

ID = "0123456789abcdef"


@pytest.fixture
def red(application, worker):
  Worker.update(subdomain="red").where(Worker.hostname == worker.hostname).execute()
  return Container.create(application=application, worker=worker)


@pytest.fixture
def linked(red):
  return Domain.create(name="shop", type="public", port=8080, container=red, service="shop")


@pytest.mark.parametrize("kind, value, owner", [
    ("file", f"domain-{ID}.yml", ID), ("dns", f"sage-domain-{ID}", ID),
    ("file", "domain-42.yml", None), ("dns", "sage-domain-42", None),
    ("file", f"domain-{ID.upper()}.yml", None), ("dns", f"sage-domain-{ID[:-1]}", None),
    ("file", f"domain-{ID}0.yml", None), ("dns", f"sage-domain--{ID}", None),
    ("file", f"their-domain-{ID}.yml", None), ("dns", f"SAGE-DOMAIN-{ID}", None),
    ("file", "certs.yml", None), ("dns", "sage-worker-w1", None),
    ("file", f"sage-domain-{ID}", None), ("dns", f"domain-{ID}.yml", None),
    ("file", f"domain-{ID}.yml.old", None), ("dns", f"sage-domain-{ID}-other", None),
    ("file", f"domain-{ID}.yml\n", None), ("dns", f"sage-domain-{ID}\n", None),
])
def test_artifact_identity_names_only_its_exact_record_owner(kind, value, owner):
  assert domain_artifact_owner(kind, value) == owner
  if owner is not None:
    assert domain_artifact_name(kind, owner) == value


@pytest.mark.parametrize("kind", ["file", "dns"])
def test_a_new_domain_id_names_artifacts_it_owns(kind):
  domain_id = new_id()
  assert domain_artifact_owner(kind, domain_artifact_name(kind, domain_id)) == domain_id


@pytest.mark.parametrize("type, expected_host, expected_address", [
    ("public", "shop.example.test", "https://shop.example.test"),
    ("internal", "shop.red.int.example.test", "https://shop.red.int.example.test"),
    ("tcp", "shop.red.int.example.test", "shop.red.int.example.test:8443"),
])
def test_its_name_is_public_or_under_its_namespace_workers_subdomain(red, worker, type, expected_host, expected_address):
  domain = Domain.create(name="shop", type=type, port=80, container=red,
                         namespace_worker=None if type == "public" else worker.hostname)
  assert (host(domain, "example.test"), address(domain, "example.test")) == (expected_host, expected_address)


def test_an_internal_domain_without_a_namespace_worker_has_no_host_address_or_origins(db):
  domain = Domain(name="shop", type="internal", port=80)
  assert (host(domain, "example.test"), address(domain, "example.test"), allowed_origins(domain, "example.test")) == (
      None, None, ())


def test_blank_origins_follow_its_own_address_and_explicit_ones_are_sorted(linked):
  assert allowed_origins(linked, "example.test") == ("https://shop.example.test",)
  linked.cors_origins = ["https://b.test", "https://a.test"]
  assert allowed_origins(linked, "example.test") == ("https://a.test", "https://b.test")


def test_tcp_has_no_origins(red, worker):
  domain = Domain.create(name="shop", type="tcp", port=80, container=red, namespace_worker=worker)
  assert allowed_origins(domain, "example.test") == ()


def test_the_internal_default_origin_follows_its_workers_subdomain(red, worker):
  domain = Domain.create(name="app", type="internal", port=80, container=red, namespace_worker=worker)
  Worker.update(subdomain="blue").where(Worker.hostname == worker.hostname).execute()
  assert allowed_origins(Domain.get_by_id(domain.id), "example.test") == ("https://app.blue.int.example.test",)


@pytest.mark.parametrize("port", [1, 80, 65535])
def test_a_port_in_range_is_accepted(port):
  assert validate_port(port) == port


@pytest.mark.parametrize("port", [0, 65536, -1, "80", True, None, 80.0])
def test_anything_else_is_refused(port):
  with pytest.raises(ValueError, match="must be a number from 1 to 65535"):
    validate_port(port)


class TestDomainConfig:
  def test_the_snapshot_is_immutable_and_does_not_follow_later_record_edits(self, linked):
    snapshot = DomainConfig.capture(linked, "example.test", "templates")
    linked.port = 9000
    linked.save()

    assert snapshot.port == 8080
    assert snapshot.content_hash != DomainConfig.capture(linked, "example.test", "templates").content_hash
    with pytest.raises(FrozenInstanceError):
      snapshot.port = 9000

  @pytest.mark.parametrize("field, value", [("name", "store"), ("port", 9000), ("type", "tcp")])
  def test_the_fingerprint_changes_with_rendered_configuration_and_the_artifact_names_do_not(self, linked, field, value):
    before = DomainConfig.capture(linked, "example.test", "templates")
    setattr(linked, field, value)
    linked.save()

    after = DomainConfig.capture(linked, "example.test", "templates")
    assert before.content_hash != after.content_hash
    assert (before.filename, before.dns_comment) == (after.filename, after.dns_comment) == (
        f"domain-{linked.id}.yml", f"sage-domain-{linked.id}")

  def test_discovery_metadata_does_not_change_the_apply_fingerprint(self, linked):
    before = DomainConfig.capture(linked, "example.test", "templates")
    linked.service, linked.pool = "other", "eu"
    linked.save()

    assert before.content_hash == DomainConfig.capture(linked, "example.test", "templates").content_hash

  def test_the_fingerprint_includes_the_domain_and_the_template(self, linked):
    before = DomainConfig.capture(linked, "example.test", "templates")
    assert before.content_hash != DomainConfig.capture(linked, "other.test", "templates").content_hash
    assert before.content_hash != DomainConfig.capture(linked, "example.test", "changed").content_hash

  def test_it_names_the_container_and_the_worker_it_routes_to(self, linked):
    snapshot = DomainConfig.capture(linked, "example.test", "templates")
    assert (snapshot.worker, snapshot.container_id) == ("w1", linked.container_id)

  def test_renaming_its_containers_application_does_not_change_the_fingerprint(self, linked, application):
    before = DomainConfig.capture(linked, "example.test", "templates")
    application.name, application.label = "store", "Store"
    application.save()
    assert before.content_hash == DomainConfig.capture(Domain.get_by_id(linked.id), "example.test", "templates").content_hash

  def test_an_unlinked_domain_routes_to_its_namespace_worker_or_nowhere(self, linked, red, worker):
    internal = Domain.create(name="api", type="internal", port=80, namespace_worker=worker.hostname)
    assert DomainConfig.capture(internal, "example.test", "templates").worker == "w1"
    linked.container = None
    linked.save()
    assert DomainConfig.capture(linked, "example.test", "templates").worker is None

  def test_the_templates_are_part_of_the_fingerprint_by_default(self, linked):
    assert DomainConfig.capture(linked, "example.test").templates == ROUTING_DIGEST

  def test_a_linked_public_domain_points_its_name_at_its_workers_tunnel(self, linked):
    snapshot = DomainConfig.capture(linked, "example.test", "templates")
    assert snapshot.dns_records({"id": "tunnel1"}) == {"shop.example.test": "tunnel1.cfargotunnel.com"}
    with pytest.raises(ValueError, match="Worker w1 has no tunnel."):
      snapshot.dns_records(None)

  def test_an_internal_or_unlinked_domain_has_no_dns_records(self, linked, worker):
    internal = Domain.create(name="api", type="internal", port=80, container=linked.container_id,
                             namespace_worker=worker.hostname)
    unlinked = Domain.create(name="old", type="public", port=80)
    for domain in (internal, unlinked):
      assert DomainConfig.capture(domain, "example.test", "templates").dns_records(None) == {}


class TestRoute:
  @pytest.mark.parametrize("type", ["public", "internal", "tcp"])
  def test_its_first_line_names_the_domain_and_the_fingerprint_it_was_rendered_from(self, red, worker, type):
    domain = Domain.create(name="shop", type=type, port=8080, container=red,
                           namespace_worker=None if type == "public" else worker.hostname)
    snapshot = DomainConfig.capture(domain, "example.test")
    route = snapshot.route()
    assert parse_route_listing([f"/opt/sage/traefik/dynamic/{snapshot.filename}:{route.splitlines()[0]}"]) == {
        domain.id: snapshot.content_hash}
    assert "${" not in route

  def test_an_http_route_sends_its_name_to_the_containers_runtime_with_its_origins(self, linked):
    linked.cors_origins = ["https://b.test", "https://a.test"]
    linked.save()
    http = yaml.safe_load(DomainConfig.capture(linked, "example.test").route())["http"]
    assert http["routers"][f"domain_{linked.id}"]["rule"] == "Host(`shop.example.test`)"
    assert http["services"][f"domain_{linked.id}"]["loadBalancer"]["servers"] == [
        {"url": f"http://sage-{linked.container_id}:8080"}]
    assert http["middlewares"][f"domain_{linked.id}_cors"]["headers"]["accessControlAllowOriginList"] == [
        "https://a.test", "https://b.test"]

  def test_a_tcp_route_sends_its_name_to_the_containers_runtime(self, red, worker):
    domain = Domain.create(name="db", type="tcp", port=5432, container=red, namespace_worker=worker.hostname)
    tcp = yaml.safe_load(DomainConfig.capture(domain, "example.test").route())["tcp"]
    assert tcp["routers"][f"domain_{domain.id}"]["rule"] == "HostSNI(`db.red.int.example.test`)"
    assert tcp["services"][f"domain_{domain.id}"]["loadBalancer"]["servers"] == [{"address": f"sage-{red.id}:5432"}]
