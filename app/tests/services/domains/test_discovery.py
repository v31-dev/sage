import itertools
import subprocess
from types import SimpleNamespace

import pytest
import yaml

import config
from services.domains.discovery import discovery_files, discovery_fingerprint, discovery_services, files_command


def member(type="public", service="shop", host="shop.example.test", pool=None, status="synced", eligible=True, ready=True):
  """A Domain with a service and its state, as discovery reads them."""
  return (SimpleNamespace(type=type, service=service, pool=pool),
          SimpleNamespace(config=SimpleNamespace(host=host), view=SimpleNamespace(status=status),
                          facts=SimpleNamespace(eligible=eligible, worker_ready=ready)))


class TestServices:
  def test_names_are_grouped_by_namespace_and_service_and_sorted(self):
    services = discovery_services([
        member(host="b.example.test"), member(host="a.example.test", pool="eu"),
        member(type="tcp", service="db", host="db.red.int.example.test"),
        member(type="internal", service="db", host="api.red.int.example.test"),
        member(type="internal", service="shop", host="shop.red.int.example.test")])
    assert services == [
        ["internal", "db", [["api.red.int.example.test", None, True], ["db.red.int.example.test", None, True]]],
        ["internal", "shop", [["shop.red.int.example.test", None, True]]],
        ["public", "shop", [["a.example.test", "eu", True], ["b.example.test", None, True]]]]

  def test_a_name_without_a_full_name_is_not_listed(self):
    assert discovery_services([member(type="internal", host=None)]) == []

  @pytest.mark.parametrize("status, eligible, ready", list(itertools.product(
      ("synced", "pending", "error", "syncing"), (True, False, None), (True, False, None))))
  def test_a_name_is_online_only_when_its_domain_is_synced_its_container_routable_and_its_worker_ready(
          self, status, eligible, ready):
    [[_, _, [[_, _, online]]]] = discovery_services([member(status=status, eligible=eligible, ready=ready)])
    assert online is (status == "synced" and eligible is True and ready is True)


class TestFingerprint:
  def test_it_follows_the_services_their_availability_and_the_domain(self):
    services = discovery_services([member()])
    fingerprints = {discovery_fingerprint(services, "example.test"), discovery_fingerprint(services, "other.test"),
                    discovery_fingerprint(discovery_services([member(ready=False)]), "example.test"),
                    discovery_fingerprint(discovery_services([member(pool="eu")]), "example.test"),
                    discovery_fingerprint([], "example.test")}
    assert len(fingerprints) == 5
    assert discovery_fingerprint(discovery_services([member()]), "example.test") == discovery_fingerprint(services, "example.test")


class TestFiles:
  def test_a_public_service_answers_on_the_public_name_with_its_endpoints(self):
    services = discovery_services([member(host="a.example.test", pool="eu"), member(host="b.example.test", ready=False)])
    [(name, content)] = discovery_files(services, "example.test").items()
    http = yaml.safe_load(content)["http"]
    router, headers = http["routers"]["discovery_public_shop"], http["middlewares"]["discovery_public_shop"]["headers"]
    assert name == "_discovery-public-shop.yml"
    assert "Host(`discovery.example.test`)" in router["rule"] and "Query(`service`, `shop`)" in router["rule"]
    assert router["entrypoints"] == ["web"] and "tls" not in router
    assert headers["customResponseHeaders"] == {
        "Sage-Endpoints": '"a.example.test";pool="eu";online, "b.example.test";online=?0',
        "Cache-Control": f"private, max-age={config.DISCOVERY_CACHE_SECONDS}"}

  def test_an_internal_service_answers_on_the_tailnet_name_over_tls(self):
    services = discovery_services([member(type="tcp", service="db", host="db.red.int.example.test")])
    [(name, content)] = discovery_files(services, "example.test").items()
    router = yaml.safe_load(content)["http"]["routers"]["discovery_internal_db"]
    assert name == "_discovery-internal-db.yml"
    assert "Host(`discovery.int.example.test`)" in router["rule"]
    assert (router["entrypoints"], router["middlewares"][0], router["tls"]) == (["websecure"], "tailscale-only", {})

  def test_no_service_has_no_file(self):
    assert discovery_files([], "example.test") == {}


class TestFilesCommand:
  def test_it_lists_the_discovery_files_of_a_real_directory_and_no_other(self, tmp_path):
    for name in ("_discovery-public-shop.yml", "_discovery-internal-db.yml", "domain-0123456789abcdef.yml", "config.yml"):
      (tmp_path / name).write_text("http:\n")
    (tmp_path / "_discovery-sub").mkdir()
    output = subprocess.run(["sh", "-c", files_command(str(tmp_path))], capture_output=True, text=True, check=True).stdout
    assert sorted(output.splitlines()) == ["_discovery-internal-db.yml", "_discovery-public-shop.yml"]

  def test_a_directory_not_there_lists_nothing(self, tmp_path):
    result = subprocess.run(["sh", "-c", files_command(str(tmp_path / "missing"))], capture_output=True, text=True)
    assert (result.returncode, result.stdout) == (0, "")
