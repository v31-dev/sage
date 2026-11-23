import pytest
import yaml

from services.db.models import Application, Container, Domain, DomainCaller, Worker
from services.domains.callers import CalledName, called_names, caller_aliases, domain_file, worker_fingerprints
from services.domains.config import DomainConfig

WEB, API, CRON = 1, 2, 3
SUBNETS = {WEB: ("172.21.0.0/16",), API: ("172.22.0.0/16", "fd00:22::/64")}
ADDRESSES = {"w1": "100.64.0.1", "w2": "100.64.0.2", "w3": None}
SUBDOMAINS = {"w1": "red", "w2": "blue", "w3": "green"}


def called(worker="w1", type="internal", callers=(WEB,), domain_id="d1"):
  host = f"{domain_id}.{SUBDOMAINS[worker]}.int.example.test"
  return CalledName(domain_id, type, host, worker, frozenset(callers))


class TestAliases:
  def test_each_applications_network_answers_to_the_names_it_may_call_that_this_worker_routes(self):
    names = (called(domain_id="d1"), called(worker="w2", domain_id="d2", type="tcp"), called(worker="w3", domain_id="d3"),
             called(domain_id="d4", callers=(API,)))
    assert caller_aliases(names, "w1", {"sage-web": WEB, "sage-cron": CRON}, ADDRESSES) == {
        "sage-web": ("d1.red.int.example.test", "d2.blue.int.example.test"), "sage-cron": ()}


class TestWorkerFingerprints:
  def config(self, callers=()):
    return DomainConfig("d1", "tcp", "pg.red.int.example.test", 5432, "w1", "c0", (), (), tuple(callers), "t")

  def test_its_own_worker_without_callers_there_carries_the_configurations_own(self):
    config = self.config()
    assert worker_fingerprints(config, {}, None) == {"w1": config.content_hash}

  def test_each_callers_worker_is_expected_and_unknown_until_its_networks_and_the_address_are_known(self):
    config = self.config([("w1", "c1", WEB), ("w2", "c2", API)])
    assert worker_fingerprints(config, {}, "100.64.0.1") == {"w1": None, "w2": None}
    assert worker_fingerprints(config, {"w1": {}, "w2": {}}, None)["w2"] is None
    known = worker_fingerprints(config, {"w1": {}, "w2": {}}, "100.64.0.1")
    assert None not in known.values() and config.content_hash not in known.values()

  def test_it_follows_the_callers_subnets_there_and_no_other_containers(self):
    config = self.config([("w2", "c2", API)])
    base = worker_fingerprints(config, {"w2": {"c2": ("172.21.0.0/16",)}}, "100.64.0.1")["w2"]
    assert worker_fingerprints(config, {"w2": {"c2": ("172.21.0.0/16",), "c9": ("172.29.0.0/16",)}}, "100.64.0.1")["w2"] == base
    assert worker_fingerprints(config, {"w2": {"c2": ("172.22.0.0/16",)}}, "100.64.0.1")["w2"] != base
    assert worker_fingerprints(config, {"w2": {"c2": ("172.21.0.0/16",)}}, "100.64.0.9")["w2"] != base


class TestDomainFile:
  def config(self, type="internal", callers=()):
    return DomainConfig("d1", type, "api.red.int.example.test", 80, "w1", "c0", (), (("X-Sage", "1"),), tuple(callers),
                        "t")

  def parsed(self, rendered):
    fingerprint, content = rendered
    header, _, body = content.partition("\n")
    return fingerprint, header, yaml.safe_load(body)

  def test_on_its_own_worker_without_callers_it_is_its_route_as_before(self):
    config = self.config()
    assert domain_file(config, "w1", {}, None) == (config.content_hash, config.route())

  def test_on_its_own_worker_its_callers_join_its_route_under_the_fingerprint_expected_there(self):
    config = self.config(callers=[("w1", "c1", WEB), ("w1", "c9", CRON)])
    networks = {"c1": ("172.21.0.0/16",), "c8": ("172.28.0.0/16",)}
    fingerprint, header, body = self.parsed(domain_file(config, "w1", networks, None))
    assert fingerprint == worker_fingerprints(config, {"w1": networks}, None)["w1"] and header == f"# sage-domain d1 {fingerprint}"
    routers = body["http"]["routers"]
    assert {"domain_d1", "domain_d1_ping", "domain_d1_callers"} <= set(routers)
    assert routers["domain_d1_callers"]["rule"] == "Host(`api.red.int.example.test`) && (ClientIP(`172.21.0.0/16`))"
    assert routers["domain_d1_callers"]["middlewares"] == ["domain_d1_headers", "domain_d1_cors"]

  def test_a_tcp_names_local_callers_end_tls_as_its_route_does(self):
    _, _, body = self.parsed(domain_file(self.config("tcp", [("w1", "c1", WEB)]), "w1", {"c1": ("172.21.0.0/16",)}, None))
    assert body["tcp"]["routers"]["domain_d1_callers"]["tls"] == {"options": "backend-tcp"}

  def test_on_a_callers_worker_it_passes_their_connections_through_and_with_none_there_routes_nothing(self):
    config = self.config(callers=[("w2", "c2", API)])
    _, _, body = self.parsed(domain_file(config, "w2", {"c2": ("172.22.0.0/16",)}, "100.64.0.1"))
    assert body["tcp"]["services"]["domain_d1_callers"] == {"loadBalancer": {"servers": [{"address": "100.64.0.1:443"}]}}
    assert body["tcp"]["routers"]["domain_d1_callers"]["tls"] == {"passthrough": True}
    assert self.parsed(domain_file(config, "w2", {}, "100.64.0.1"))[2] == {}
    assert domain_file(config, "w2", {"c2": ("172.22.0.0/16",)}, None) is None


class TestCalledNames:
  def test_each_internal_or_tcp_name_with_callers_is_listed_with_its_worker_and_applications(self, application, worker):
    Worker.update(subdomain="red").where(Worker.hostname == worker.hostname).execute()
    other = Application.create(project=application.project, name="api", label="API", image="nginx")
    container = Container.create(application=application, worker=worker)
    for name, type in (("api", "internal"), ("db", "tcp"), ("plain", "internal")):
      Domain.create(id=f"{name:0>16}", name=name, type=type, port=80, container=container, namespace_worker=worker)
    for domain_id in ("0000000000000api", "00000000000000db"):
      DomainCaller.create(domain=domain_id, application=application)
    DomainCaller.create(domain="0000000000000api", application=other)
    assert [caller[1:] for caller in DomainConfig.capture(Domain.get_by_id("0000000000000api"), "example.test").callers] == sorted(
        [(container.id, application.id)])
    assert called_names("example.test") == (
        CalledName("00000000000000db", "tcp", "db.red.int.example.test", "w1", frozenset({application.id})),
        CalledName("0000000000000api", "internal", "api.red.int.example.test", "w1", frozenset({application.id, other.id})))
