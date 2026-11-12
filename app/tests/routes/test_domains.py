import pytest

from services.db.models import Container, Domain, Worker

URL = "/api/domains"


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


@pytest.fixture
def domain(container):
  return Domain.create(name="shop", type="public", port=8080, container=container)


@pytest.fixture
def payloads(manager):
  manager.answers["domain_payloads"] = lambda domains: [{"id": domain.id} for domain in domains]


class TestList:
  def test_it_is_the_managers_page(self, client, manager):
    assert client.get(f"{URL}/").status_code == 200
    assert manager.called("domains_page") == [((), {})]


class TestCreate:
  def test_a_domain_is_linked_to_its_container_its_apply_asked_and_its_payload_answered(
          self, client, container, manager, payloads):
    response = client.post(f"{URL}/", json={"container": container.id, "name": "shop", "type": "public", "port": 8080})
    domain = Domain.get(Domain.name == "shop")
    assert (response.json(), domain.container_id) == ({"id": domain.id}, container.id)
    assert manager.called("request_domain_applies") == [(([domain.id],), {})]

  @pytest.mark.parametrize("linked, fields, status", [
      (None, {"name": "shop"}, 400), ("c0000000000000000", {"name": "shop"}, 404), ("own", {"name": "taken"}, 409),
      ("own", {"name": "shop", "port": 0}, 400), ("own", {"name": "shop", "status": "synced"}, 400)])
  def test_what_the_command_refuses_is_its_status_and_asks_for_nothing(self, client, container, manager, payloads,
                                                                       linked, fields, status):
    Domain.create(name="taken", type="public", port=80, container=container)
    link = {} if linked is None else {"container": container.id if linked == "own" else linked}
    assert client.post(f"{URL}/", json={**link, "type": "public", "port": 80, **fields}).status_code == status
    assert Domain.select().count() == 1
    assert manager.calls == []


class TestEdit:
  def test_an_edit_is_written_its_follow_ups_asked_and_its_payload_answered(self, client, domain, manager, payloads):
    assert client.put(f"{URL}/{domain.id}", json={"port": 9090}).json() == {"id": domain.id}
    assert Domain.get_by_id(domain.id).port == 9090
    assert manager.called("domain_edited") == [((domain.id,), {})]

  def test_another_container_relinks_it(self, client, domain, application, manager, payloads):
    other = Container.create(application=application, worker=Worker.create(hostname="w2", subdomain="blue"))
    client.put(f"{URL}/{domain.id}", json={"container": other.id})
    assert Domain.get_by_id(domain.id).container_id == other.id

  @pytest.mark.parametrize("target, body, status", [
      ("missing", {"port": 9090}, 404), ("own", {"container": "c0000000000000000"}, 404),
      ("own", {"port": "often"}, 400), ("own", {"name": "taken"}, 409)])
  def test_what_the_command_refuses_is_its_status_and_asks_for_nothing(self, client, domain, container, manager,
                                                                       payloads, target, body, status):
    Domain.create(name="taken", type="public", port=80, container=container)
    assert client.put(f"{URL}/{domain.id if target == 'own' else target}", json=body).status_code == status
    assert (Domain.get_by_id(domain.id).port, Domain.get_by_id(domain.id).name) == (8080, "shop")
    assert manager.calls == []


class TestDelete:
  def test_a_domain_is_deleted_with_no_content_and_the_pass_asked_for(self, client, domain, manager):
    assert client.delete(f"{URL}/{domain.id}").status_code == 204
    assert Domain.select().count() == 0
    assert manager.called("request_domain_reconciliation") == [((), {})]

  def test_an_unknown_domain_is_a_404_and_asks_for_nothing(self, client, domain, manager):
    assert client.delete(f"{URL}/missing").status_code == 404
    assert manager.calls == []
