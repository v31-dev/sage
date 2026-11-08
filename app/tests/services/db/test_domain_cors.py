import pytest
import yaml

from services.db import Container, Database, Domain, Worker
from services.domains import DomainConfig
from tests.services.manager.test_domain_reconciliation import applied, manager


@pytest.fixture
def domain(application, worker):
  return Domain.create(name="app", type="public", port=80,
                       container=Container.create(application=application, worker=worker, status="active"))


def test_blank_stays_null_and_follows_its_own_name(domain):
  assert domain.cors_origins is None
  assert domain.allowed_origins("example.test") == ("https://app.example.test",)

  domain.name = "frontend"
  domain.save()

  assert Domain.get_by_id(domain.id).cors_origins is None
  assert domain.allowed_origins("example.test") == ("https://frontend.example.test",)


def test_explicit_references_are_not_rewritten_when_another_domain_is_renamed(domain):
  api = Domain.create(name="app-api-1", type="public", port=80, container=domain.container,
                      service="app-api", cors_origins=["https://app.example.test"])
  before = DomainConfig.capture(api, "example.test", "templates").content_hash

  domain.name = "frontend"
  domain.save()

  stored = Domain.get_by_id(api.id)
  assert stored.cors_origins == ["https://app.example.test"]
  assert DomainConfig.capture(stored, "example.test", "templates").content_hash == before


def test_internal_default_tracks_worker_subdomain_and_placement(domain):
  domain.type = "internal"
  domain.save()
  Worker.update(subdomain="red").where(Worker.hostname == "w1").execute()
  domain = Domain.get_by_id(domain.id)
  assert domain.allowed_origins("example.test") == ("https://app.red.int.example.test",)
  Worker.update(subdomain="blue").where(Worker.hostname == "w1").execute()
  assert Domain.get_by_id(domain.id).allowed_origins("example.test") == ("https://app.blue.int.example.test",)


def test_resetting_an_explicit_list_restores_the_implicit_origin(domain):
  domain.cors_origins = ["https://frontend.example.test"]
  domain.save()
  domain.cors_origins = []
  domain.save()

  assert Domain.get_by_id(domain.id).cors_origins is None
  assert domain.allowed_origins("example.test") == ("https://app.example.test",)


def test_bulk_writes_are_validated_without_corrupting_the_stored_policy(domain):
  with pytest.raises(ValueError):
    Domain.update(cors_origins=["https://app.example.test/path"]).where(Domain.id == domain.id).execute()
  assert Domain.get_by_id(domain.id).cors_origins is None


def test_saving_an_unrelated_field_does_not_overwrite_a_newer_origins_edit(domain):
  other = Domain.get_by_id(domain.id)
  other.cors_origins = ["https://frontend.example.test"]
  other.save()

  domain.port = 8080
  domain.save()

  stored = Domain.get_by_id(domain.id)
  assert stored.port == 8080
  assert stored.cors_origins == ["https://frontend.example.test"]


def test_tcp_has_no_effective_policy_and_clears_the_http_override(domain):
  domain.type = "tcp"
  domain.cors_origins = ["https://frontend.example.test"]
  domain.save()

  assert domain.allowed_origins("example.test") == ()
  assert Domain.get_by_id(domain.id).cors_origins is None


def test_schema_adds_the_nullable_column_without_losing_bindings(domain, db):
  db.execute_sql("ALTER TABLE domain DROP COLUMN cors_origins")

  Database().reconcile_schema()

  stored = Domain.get_by_id(domain.id)
  assert stored.container_id == domain.container_id
  assert stored.cors_origins is None
  assert stored.allowed_origins("example.test") == ("https://app.example.test",)


def test_schema_preserves_an_explicit_policy(domain):
  domain.cors_origins = ["https://frontend.example.test"]
  domain.save()

  Database().reconcile_schema()

  assert Domain.get_by_id(domain.id).cors_origins == ["https://frontend.example.test"]


@pytest.mark.parametrize("type", ["public", "internal"])
@pytest.mark.parametrize("origins", [None, ["https://frontend.example.test", "http://localhost:5173"], ["*"], ["null"]])
def test_the_record_file_owns_the_effective_policy_and_proof(manager, application, type, origins):
  domain = applied(manager, application, type=type)
  domain.cors_origins = origins
  domain.save()

  manager.run(domain.id, application.id)

  stored = Domain.get_by_id(domain.id)
  rendered = yaml.safe_load(manager.tailscale.files("w1")[f"domain-{domain.id}.yml"])["http"]
  headers = rendered["middlewares"][f"domain_{domain.id}_cors"]["headers"]
  assert headers["accessControlAllowOriginList"] == list(stored.allowed_origins("example.test"))
  assert headers["customResponseHeaders"]["Access-Control-Allow-Origin"] == ""
  assert rendered["routers"][f"domain_{domain.id}"]["middlewares"][-1] == f"domain_{domain.id}_cors"
  assert stored.synced_hash == manager.domain_config(stored).content_hash
  assert stored.cors_origins == origins


def test_cors_changes_invalidate_proof_and_mid_apply_edits_cannot_be_certified(manager, application):
  domain = applied(manager, application)
  before = domain.synced_hash
  Domain.update(cors_origins=["https://frontend.example.test"]).where(Domain.id == domain.id).execute()
  changed = Domain.get_by_id(domain.id)
  assert manager.domain_config(changed).content_hash != before
  manager.cloudflare.callback = lambda: Domain.update(cors_origins=["https://other.example.test"]).where(Domain.id == domain.id).execute()

  manager.run(domain.id, application.id)

  stored = Domain.get_by_id(domain.id)
  assert stored.status == "pending" and stored.synced_hash == before
  assert manager.has_task(manager.sync_domain, {"domain_id": domain.id})


def test_list_order_does_not_change_the_effective_proof(domain):
  domain.cors_origins = ["https://b.example.test", "https://a.example.test"]
  domain.save()
  before = DomainConfig.capture(domain, "example.test", "templates").content_hash
  domain.cors_origins.reverse()
  assert DomainConfig.capture(domain, "example.test", "templates").content_hash == before


def test_origins_are_serialized_as_data_inside_yaml(manager, application):
  domain = applied(manager, application)
  domain.cors_origins = ["https://foo'bar.example.test"]
  domain.save()

  manager.run(domain.id, application.id)

  text = manager.tailscale.files("w1")[f"domain-{domain.id}.yml"]
  assert yaml.safe_load(text)["http"]["middlewares"][f"domain_{domain.id}_cors"]["headers"]["accessControlAllowOriginList"] == domain.cors_origins
