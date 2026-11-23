import pytest
import yaml

from services.db.models import Application, Container, Domain, DomainCaller, Worker
from services.domains.callers import MARKER, CalledName, called_names, caller_aliases, render_callers

WEB, API, CRON = 1, 2, 3
SUBNETS = {WEB: ("172.21.0.0/16",), API: ("172.22.0.0/16", "fd00:22::/64")}
ADDRESSES = {"w1": "100.64.0.1", "w2": "100.64.0.2", "w3": None}
SUBDOMAINS = {"w1": "red", "w2": "blue", "w3": "green"}


def called(worker="w1", type="internal", callers=(WEB,), domain_id="d1"):
  host = f"{domain_id}.{SUBDOMAINS[worker]}.int.example.test"
  return CalledName(domain_id, type, host, worker, frozenset(callers))


def render(*names, worker="w1", subnets=SUBNETS):
  text = render_callers(names, worker, subnets, ADDRESSES)
  return None if text is None else yaml.safe_load(text)


class TestRender:
  def test_a_name_on_this_worker_lets_the_allowed_applications_networks_here_through_to_its_own_service(self):
    config = render(called(callers=(WEB, API, CRON)))
    assert config == {"http": {"routers": {"caller_d1": {
        "rule": "Host(`d1.red.int.example.test`) && (ClientIP(`172.21.0.0/16`) || ClientIP(`172.22.0.0/16`) "
                "|| ClientIP(`fd00:22::/64`))",
        "entryPoints": ["websecure"], "middlewares": ["domain_d1_headers@file", "domain_d1_cors@file"],
        "service": "domain_d1@file", "tls": {}}}}}

  def test_a_tcp_name_on_this_worker_ends_tls_as_its_own_route_does(self):
    config = render(called(type="tcp"))
    assert config == {"tcp": {"routers": {"caller_d1": {
        "rule": "HostSNI(`d1.red.int.example.test`) && (ClientIP(`172.21.0.0/16`))", "entryPoints": ["tcps"],
        "service": "domain_d1@file", "tls": {"options": "backend-tcp"}}}}}

  @pytest.mark.parametrize("type, entry_point, port", [("internal", "websecure", 443), ("tcp", "tcps", 8443)])
  def test_a_name_on_another_worker_passes_through_to_that_workers_address(self, type, entry_point, port):
    config = render(called(worker="w2", type=type))
    assert config == {"tcp": {
        "routers": {"caller_d1": {"rule": "HostSNI(`d1.blue.int.example.test`) && (ClientIP(`172.21.0.0/16`))",
                                  "entryPoints": [entry_point], "service": "caller_d1", "tls": {"passthrough": True}}},
        "services": {"caller_d1": {"loadBalancer": {"servers": [{"address": f"100.64.0.2:{port}"}]}}}}}

  def test_a_name_whose_worker_has_no_known_address_is_left_out_until_it_has_one(self):
    assert render(called(worker="w3")) is None
    assert set(render(called(worker="w3"), called(worker="w2", domain_id="d2"))["tcp"]["routers"]) == {"caller_d2"}

  def test_a_name_with_no_allowed_application_here_has_no_router_and_none_at_all_gives_no_file(self):
    assert render(called(callers=(CRON,))) is None
    assert render(called(), subnets={}) is None

  def test_its_first_line_is_the_fingerprint_of_the_rest_which_changes_with_it(self):
    first = render_callers((called(),), "w1", SUBNETS, ADDRESSES)
    second = render_callers((called(callers=(WEB, API)),), "w1", SUBNETS, ADDRESSES)
    assert first.startswith(f"{MARKER} ") and first.splitlines()[0] != second.splitlines()[0]
    assert first == render_callers((called(),), "w1", SUBNETS, ADDRESSES)


class TestAliases:
  def test_each_applications_network_answers_to_the_names_it_may_call_that_this_worker_routes(self):
    names = (called(domain_id="d1"), called(worker="w2", domain_id="d2", type="tcp"), called(worker="w3", domain_id="d3"),
             called(domain_id="d4", callers=(API,)))
    assert caller_aliases(names, "w1", {"sage-web": WEB, "sage-cron": CRON}, ADDRESSES) == {
        "sage-web": ("d1.red.int.example.test", "d2.blue.int.example.test"), "sage-cron": ()}


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
    assert called_names("example.test") == (
        CalledName("00000000000000db", "tcp", "db.red.int.example.test", "w1", frozenset({application.id})),
        CalledName("0000000000000api", "internal", "api.red.int.example.test", "w1", frozenset({application.id, other.id})))
