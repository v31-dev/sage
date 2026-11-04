import pytest

from services.db import Application, Container, Domain, Worker

URL = "/api/domains"


@pytest.fixture
def red(application, worker):
  Worker.update(subdomain="red").where(Worker.hostname == "w1").execute()
  return Container.create(application=application, worker=worker)


@pytest.fixture
def blue(project):
  other = Application.create(project=project, name="api", label="API", image="nginx")
  return Container.create(application=other, worker=Worker.create(hostname="w2", ip="100.64.0.3", subdomain="blue"))


class TestList:
  def test_it_holds_every_domain_with_its_container_and_every_container_to_link_one_to(self, client, red, blue, manager):
    Domain.create(name="shop", type="internal", port=80, container=red, service="shop", pool="eu")

    body = client.get(f"{URL}/").json()

    assert body["domains"] == [{
        "id": 1, "name": "shop", "type": "internal", "port": 80, "service": "shop", "pool": "eu",
        "host": "shop.red.int.example.test", "address": "https://shop.red.int.example.test", "worker": "w1",
        "container": {"id": red.id, "status": "inactive", "worker": "w1",
                      "application": {"name": "web", "label": "Web", "project": {"name": "demo", "label": "Demo"}}},
    }]
    assert [(container["worker"], container["application"]["name"]) for container in body["containers"]] == [
        ({"hostname": "w1", "subdomain": "red"}, "web"), ({"hostname": "w2", "subdomain": "blue"}, "api")]

  def test_an_unlinked_internal_domain_keeps_its_name_and_worker(self, client, red, manager):
    Domain.create(name="shop", type="internal", port=80, container=red)
    red.delete_instance()

    (domain,) = client.get(f"{URL}/").json()["domains"]

    assert (domain["container"], domain["worker"], domain["host"]) == (None, "w1", "shop.red.int.example.test")


class TestCreate:
  def test_a_domain_is_linked_to_its_container_and_its_applications_routing_and_discovery_are_resynced(
          self, client, red, manager):
    response = client.post(f"{URL}/", json={"name": "shop", "type": "public", "port": 80, "container": red.id,
                                            "service": "", "pool": " eu "})

    assert response.status_code == 200, response.json()
    assert (response.json()["host"], response.json()["service"], response.json()["pool"]) == (
        "shop.example.test", None, "eu")
    assert (manager.traefik_syncs, manager.discovery_syncs) == ([red.application_id], 1)

  def test_one_without_a_container_is_refused(self, client, red, manager):
    response = client.post(f"{URL}/", json={"name": "shop", "type": "public", "port": 80})
    assert (response.status_code, response.json()["detail"]) == (400, "A domain without a container is not allowed.")

  def test_an_unknown_container_is_not_found(self, client, red, manager):
    assert client.post(f"{URL}/", json={"name": "shop", "type": "public", "port": 80, "container": 99}).status_code == 404

  @pytest.mark.parametrize("data", [{"name": "Shop"}, {"name": "core"}, {"service": "a_b"}, {"pool": "-eu"}])
  def test_a_name_service_or_pool_the_model_refuses_is_refused_and_nothing_is_resynced(self, client, red, manager, data):
    response = client.post(f"{URL}/", json={"name": "shop", "type": "public", "port": 80, "container": red.id, **data})
    assert response.status_code == 400
    assert (Domain.select().count(), manager.traefik_syncs) == (0, [])

  def test_a_name_taken_in_its_namespace_is_refused(self, client, red, manager):
    Domain.create(name="shop", type="internal", port=80, container=red)
    response = client.post(f"{URL}/", json={"name": "shop", "type": "tcp", "port": 5432, "container": red.id})
    assert (response.status_code, response.json()["detail"]) == (
        400, "'shop' is already an internal or TCP domain on worker w1.")


class TestUpdate:
  def test_another_container_on_another_worker_moves_an_internal_name_there_and_both_applications_resync(
          self, client, red, blue, manager):
    domain = Domain.create(name="shop", type="internal", port=80, container=red)

    response = client.put(f"{URL}/{domain.id}", json={"container": blue.id})

    assert (response.json()["host"], response.json()["worker"]) == ("shop.blue.int.example.test", "w2")
    assert sorted(manager.traefik_syncs) == sorted([red.application_id, blue.application_id])

  def test_what_is_not_sent_stays_and_an_unlinked_domain_stays_unlinked(self, client, red, manager):
    domain = Domain.create(name="shop", type="internal", port=80, container=red, service="shop")
    red.delete_instance()

    response = client.put(f"{URL}/{domain.id}", json={"port": 8080})

    assert (response.json()["port"], response.json()["service"], response.json()["container"]) == (8080, "shop", None)

  def test_an_unknown_domain_is_not_found(self, client, manager):
    assert client.put(f"{URL}/9", json={"port": 80}).status_code == 404


class TestDelete:
  def test_a_domain_goes_and_its_applications_routing_and_discovery_are_resynced(self, client, red, manager):
    domain = Domain.create(name="shop", type="public", port=80, container=red)

    assert client.delete(f"{URL}/{domain.id}").status_code == 200

    assert Domain.select().count() == 0
    assert (manager.traefik_syncs, manager.discovery_syncs) == ([red.application_id], 1)
