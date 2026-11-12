import itertools
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

import config
from services.domains.discovery import Published, discovery_files, discovery_services, files_command, parse_file_listing

STARTED = datetime(2026, 1, 1, 12)


def member(type="public", service="shop", host="shop.example.test", pool=None, status="synced", reason=None, linked=True,
           eligible=True, ready=True, syncing_since=None):
  """A Domain with a service and its state, as discovery reads them."""
  return (SimpleNamespace(type=type, service=service, pool=pool),
          SimpleNamespace(config=SimpleNamespace(host=host), view=SimpleNamespace(status=status, reason=reason),
                          facts=SimpleNamespace(linked=linked, eligible=eligible, worker_ready=ready, syncing_since=syncing_since)))


def services(members, published=None, now=STARTED, started_at=STARTED):
  return discovery_services(members, published or {}, now, started_at)


def online_of(given, published=None, now=STARTED):
  [[_, _, [[_, _, online]]]] = services([member(**given)], published, now)
  return online


NOT_KNOWN = [{"status": "syncing", "syncing_since": STARTED}, {"status": "pending", "reason": "not_applied"},
             {"status": "pending", "reason": "eligibility_unknown", "eligible": None}, {"eligible": None}, {"ready": None}]
KNOWN = [({}, True), ({"linked": False, "eligible": None}, False), ({"eligible": False}, False), ({"ready": False}, False),
         ({"status": "error", "reason": "route"}, False), ({"status": "pending", "reason": "drifted"}, False),
         ({"status": "pending", "reason": "worker_not_ready", "ready": False}, False),
         ({"status": "syncing", "syncing_since": STARTED, "ready": False}, False),
         ({"status": "pending", "reason": "not_applied", "eligible": False}, False)]


class TestServices:
  def test_names_are_grouped_by_namespace_and_service_and_sorted(self):
    assert services([
        member(host="b.example.test"), member(host="a.example.test", pool="eu"),
        member(type="tcp", service="db", host="db.red.int.example.test"),
        member(type="internal", service="db", host="api.red.int.example.test"),
        member(type="internal", service="shop", host="shop.red.int.example.test")]) == [
        ["internal", "db", [["api.red.int.example.test", None, True], ["db.red.int.example.test", None, True]]],
        ["internal", "shop", [["shop.red.int.example.test", None, True]]],
        ["public", "shop", [["a.example.test", "eu", True], ["b.example.test", None, True]]]]

  def test_a_name_without_a_full_name_is_not_listed(self):
    assert services([member(type="internal", host=None)]) == []

  @pytest.mark.parametrize("given, online", KNOWN)
  def test_a_name_known_good_is_online_and_one_known_bad_offline_whatever_the_worker_publishes(self, given, online):
    assert online_of(given, {"shop.example.test": not online}) is online

  @pytest.mark.parametrize("given", NOT_KNOWN)
  def test_a_name_not_known_yet_keeps_what_the_worker_publishes_for_it_and_is_offline_where_it_publishes_nothing(
          self, given):
    assert (online_of(given, {"shop.example.test": True}), online_of(given, {"shop.example.test": False}),
            online_of(given, {"other.example.test": True})) == (True, False, False)

  @pytest.mark.parametrize("given", [given for given in NOT_KNOWN if "syncing_since" not in given])
  def test_a_name_not_known_is_offline_once_the_manager_has_run_for_the_hold(self, given):
    held = STARTED + timedelta(seconds=config.UNKNOWN_HOLD_SECONDS)
    assert online_of(given, {"shop.example.test": True}, held - timedelta(seconds=1)) is True
    assert online_of(given, {"shop.example.test": True}, held) is False

  def test_a_name_whose_apply_is_under_way_is_held_from_when_it_was_queued(self):
    queued = STARTED + timedelta(minutes=30)
    given = {"status": "syncing", "syncing_since": queued}
    assert online_of(given, {"shop.example.test": True}, queued + timedelta(seconds=config.UNKNOWN_HOLD_SECONDS - 1)) is True
    assert online_of(given, {"shop.example.test": True}, queued + timedelta(seconds=config.UNKNOWN_HOLD_SECONDS)) is False

  @pytest.mark.parametrize("status, reason, linked, eligible, ready", list(itertools.product(
      ("synced", "pending", "error", "syncing"), (None, "drifted", "not_applied"), (True, False), (True, False, None),
      (True, False, None))))
  def test_a_name_is_online_only_when_known_good_or_held_as_published_and_offline_whenever_one_part_is_known_bad(
          self, status, reason, linked, eligible, ready):
    given = {"status": status, "reason": reason, "linked": linked, "eligible": eligible, "ready": ready,
             "syncing_since": STARTED if status == "syncing" else None}
    good = status == "synced" and linked and eligible is True and ready is True
    assert (online_of(given) is True) == good
    if not linked or eligible is False or ready is False or status == "error":
      assert online_of(given, {"shop.example.test": True}) is False


def fingerprint(members, published=None, domain="example.test"):
  [(fingerprint, _)] = discovery_files(services(members, published), domain).values()
  return fingerprint


class TestFingerprint:
  def test_it_follows_the_names_their_availability_their_pools_and_the_domain(self):
    fingerprints = {fingerprint([member()]), fingerprint([member()], domain="other.test"), fingerprint([member(ready=False)]),
                    fingerprint([member(pool="eu")]), fingerprint([member(), member(host="b.example.test")])}
    assert len(fingerprints) == 5
    assert fingerprint([member()]) == fingerprint([member()])

  def test_a_name_not_known_yet_counts_with_the_availability_it_keeps(self):
    assert fingerprint([member(ready=None)], {"shop.example.test": True}) == fingerprint([member()])
    assert fingerprint([member(ready=None)]) == fingerprint([member(ready=False)])

  def test_it_is_each_services_own(self):
    files = discovery_files(services([member(), member(service="db", ready=False)]), "example.test")
    alone = discovery_files(services([member()]), "example.test")
    assert files["_discovery-public-shop.yml"] == alone["_discovery-public-shop.yml"]

  def test_the_first_line_of_the_file_names_it(self):
    [(fingerprint_, content)] = discovery_files(services([member()]), "example.test").values()
    assert content.splitlines()[0] == f"# sage-discovery {fingerprint_}"


class TestFiles:
  def test_a_public_service_answers_on_the_public_name_with_its_endpoints(self):
    found = services([member(host="a.example.test", pool="eu"), member(host="b.example.test", ready=False)])
    [(name, (_, content))] = discovery_files(found, "example.test").items()
    http = yaml.safe_load(content)["http"]
    router, headers = http["routers"]["discovery_public_shop"], http["middlewares"]["discovery_public_shop"]["headers"]
    assert name == "_discovery-public-shop.yml"
    assert "Host(`discovery.example.test`)" in router["rule"] and "Query(`service`, `shop`)" in router["rule"]
    assert router["entrypoints"] == ["web"] and "tls" not in router
    assert headers["customResponseHeaders"] == {
        "Sage-Endpoints": '"a.example.test";pool="eu";online, "b.example.test";online=?0',
        "Cache-Control": f"private, max-age={config.DISCOVERY_CACHE_SECONDS}"}

  def test_an_internal_service_answers_on_the_tailnet_name_over_tls(self):
    found = services([member(type="tcp", service="db", host="db.red.int.example.test")])
    [(name, (_, content))] = discovery_files(found, "example.test").items()
    router = yaml.safe_load(content)["http"]["routers"]["discovery_internal_db"]
    assert name == "_discovery-internal-db.yml"
    assert "Host(`discovery.int.example.test`)" in router["rule"]
    assert (router["entrypoints"], router["middlewares"][0], router["tls"]) == (["websecure"], "tailscale-only", {})

  def test_no_service_has_no_file(self):
    assert discovery_files([], "example.test") == {}

  def test_every_service_has_its_file_whatever_is_known_of_its_names(self):
    files = discovery_files(services([member(ready=None), member(service="db", eligible=None)]), "example.test")
    assert set(files) == {"_discovery-public-shop.yml", "_discovery-public-db.yml"}


class TestFilesCommand:
  def test_it_reads_each_discovery_files_proof_and_availability_in_a_real_directory_and_no_other_file(self, tmp_path):
    [(fingerprint_, content)] = discovery_files(services([member(host="a.example.test", pool="eu"),
                                                          member(host="b.example.test", ready=False)]), "example.test").values()
    (tmp_path / "_discovery-public-shop.yml").write_text(content)
    old = discovery_files(services([member(type="internal", service="db", host="db.red.int.example.test")]), "example.test")
    (tmp_path / "_discovery-internal-db.yml").write_text(old["_discovery-internal-db.yml"][1].split("\n", 1)[1])
    for name, text in (("domain-0123456789abcdef.yml", "# sage-domain\nSage-Endpoints: '\"x.example.test\";online'\n"),
                       ("config.yml", "http: {}\n")):
      (tmp_path / name).write_text(text)
    (tmp_path / "_discovery-sub").mkdir()
    output = subprocess.run(["sh", "-c", files_command(str(tmp_path))], capture_output=True, text=True, check=True).stdout
    assert parse_file_listing(output.splitlines()) == {
        "_discovery-public-shop.yml": Published(fingerprint_, {"a.example.test": True, "b.example.test": False}),
        "_discovery-internal-db.yml": Published(None, {"db.red.int.example.test": True})}

  def test_a_directory_not_there_lists_nothing(self, tmp_path):
    result = subprocess.run(["sh", "-c", files_command(str(tmp_path / "missing"))], capture_output=True, text=True)
    assert (result.returncode, result.stdout) == (0, "")

  @pytest.mark.parametrize("first", ["# sage-discovery", "# sage-discovery 0123", "# sage-discovery 0123456789ABCDEF",
                                     "# sage-domain 0123456789abcdef"])
  def test_a_first_line_that_is_not_a_header_has_no_fingerprint(self, first):
    assert parse_file_listing([f"/x/_discovery-public-shop.yml:{first}"]) == {"_discovery-public-shop.yml": Published(None, {})}

  def test_only_a_name_with_its_availability_is_read(self):
    line = '/x/_discovery-public-shop.yml:      Sage-Endpoints: \'"a.example.test";pool="eu", "b";online, nope, "c";online=?0\''
    assert parse_file_listing([line]) == {"_discovery-public-shop.yml": Published(None, {"b": True, "c": False})}
