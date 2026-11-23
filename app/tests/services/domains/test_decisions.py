import itertools
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from services.domains.config import ROUTING_TEMPLATES, domain_artifact_name, parse_route_listing, routes_command
from services.domains.decisions import ROUTES, STATUSES, ApplyOutcome, DomainFacts, RouteObservation, WorkerRoute, decide_domain, route_goal

NOW_HASH, OLD_HASH = "a" * 64, "b" * 64
QUEUED = datetime(2026, 1, 1, 12)
ID = "0123456789abcdef"
TEMPLATES = Path(__file__).parent.parent.parent.parent / "templates" / "worker" / "traefik"
REASONS = {None, "drifted", "worker_not_ready", "eligibility_unknown", "not_applied", "Traefik", "DNS"}


def facts(eligible=True, worker_ready=True, route=RouteObservation("present", NOW_HASH), dns=True, outcome=None,
          syncing_since=None):
  return DomainFacts(NOW_HASH, eligible, worker_ready, (WorkerRoute("w1", NOW_HASH, route, worker_ready),), dns, outcome,
                     syncing_since)


APPLIED = RouteObservation("present", NOW_HASH)
ABSENT = RouteObservation("absent")
FAILED_DNS = ApplyOutcome(NOW_HASH, False, "DNS", "Cloudflare: unavailable")

SCENARIOS = {
    "just after a restart, nothing observed": (facts(eligible=None, route=RouteObservation("unobserved"), dns=None), "pending",
                                               None, "eligibility_unknown"),
    "routed, its file current and DNS matching": (facts(), "synced", True, None),
    "its file from an older configuration": (facts(route=RouteObservation("present", OLD_HASH)), "pending", False, "drifted"),
    "its file without its own header": (facts(route=RouteObservation("present")), "pending", False, "drifted"),
    "routable but its file missing": (facts(route=ABSENT), "pending", False, "drifted"),
    "its container stopped and the file gone": (facts(eligible=False, route=ABSENT), "synced", True, None),
    "its container stopped and the file still there": (facts(eligible=False), "pending", False, "drifted"),
    "eligibility not known and a file present": (facts(eligible=None), "pending", None, "eligibility_unknown"),
    "the apply of this configuration failed at DNS": (facts(route=ABSENT, dns=False, outcome=FAILED_DNS), "error", False, "DNS"),
    "an older configuration failed, this one not applied yet": (
        facts(route=RouteObservation("unobserved"), dns=None, outcome=ApplyOutcome(OLD_HASH, False, "DNS", "x")), "pending", None,
        "not_applied"),
    "applied just now, not scanned since": (facts(route=RouteObservation("unobserved"), dns=None,
                                                  outcome=ApplyOutcome(NOW_HASH, True)), "synced", None, None),
    "applied, then the file disappeared": (facts(route=ABSENT, outcome=ApplyOutcome(NOW_HASH, True)), "pending", False, "drifted"),
    "a caller's file not known yet, this configuration applied": (
        DomainFacts(NOW_HASH, True, True, (WorkerRoute("w1", NOW_HASH, APPLIED, True),
                                           WorkerRoute("w2", None, RouteObservation("present", OLD_HASH), True)), True,
                    ApplyOutcome(NOW_HASH, True)), "synced", None, None),
    "a caller's file drifted": (
        DomainFacts(NOW_HASH, True, True, (WorkerRoute("w1", NOW_HASH, APPLIED, True),
                                           WorkerRoute("w2", "c" * 64, RouteObservation("present", OLD_HASH), True)), True),
        "pending", False, "drifted"),
    "a caller's worker offline": (
        DomainFacts(NOW_HASH, True, True, (WorkerRoute("w1", NOW_HASH, APPLIED, True),
                                           WorkerRoute("w2", "c" * 64, RouteObservation("unreachable"), False)), True),
        "pending", None, "worker_not_ready"),
    "its worker offline": (facts(worker_ready=False, route=RouteObservation("unreachable"), dns=True), "pending", None,
                           "worker_not_ready"),
    "a sync under way": (facts(route=ABSENT, outcome=FAILED_DNS, syncing_since=QUEUED), "syncing", False, None),
    "DNS that does not match": (facts(dns=False), "pending", False, "drifted"),
    "a failed apply since put right": (facts(outcome=FAILED_DNS), "synced", True, None),
}


@pytest.mark.parametrize("given, status, conforms, reason", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_the_decision_tables(given, status, conforms, reason):
  view = decide_domain(given)
  assert (view.status, view.conforms, view.reason) == (status, conforms, reason)


def test_an_error_says_why():
  assert decide_domain(facts(route=ABSENT, outcome=FAILED_DNS)).error == "Cloudflare: unavailable"


@pytest.mark.parametrize("eligible, goal", [(True, "present"), (False, "absent"), (None, None)])
def test_the_route_goal_removes_nothing_while_eligibility_is_unknown(eligible, goal):
  assert route_goal(eligible) == goal


def every_route():
  yield from (RouteObservation(kind) for kind in ("unobserved", "absent", "unreachable"))
  yield from (RouteObservation("present", fingerprint) for fingerprint in (NOW_HASH, OLD_HASH, None))


def expected_conformance(given):
  """Conformance worked out on its own: each worker's file must match the goal, the expected fingerprint when there is
  one to match and it is known, and DNS must match, three-valued."""
  wanted = None if given.eligible is None else given.eligible
  results = []
  for route in given.routes:
    seen = route.observed
    if wanted is None or seen.kind in ("unobserved", "unreachable"):
      results.append(None)
    elif not wanted:
      results.append(seen.kind == "absent")
    else:
      results.append(None if route.expected is None else seen.kind == "present" and seen.fingerprint == route.expected)
  results.append(given.dns_conforms)
  if False in results:
    return False
  return None if None in results else True


def caller_routes():
  """No caller worker, or one whose file is expected with a fingerprint known or not, seen in each way, ready or not."""
  yield ()
  for expected in ("c" * 64, None):
    for seen in (RouteObservation("present", "c" * 64), RouteObservation("present", OLD_HASH), RouteObservation("absent"),
                 RouteObservation("unreachable")):
      for ready in (True, False):
        yield (WorkerRoute("w2", expected, seen, ready),)


def test_every_combination_of_facts_keeps_the_invariants():
  outcomes = [
      None, ApplyOutcome(
          NOW_HASH, True), FAILED_DNS, ApplyOutcome(
          NOW_HASH, False, "Traefik", "x"), ApplyOutcome(
          OLD_HASH, False, "Traefik", "x"), ApplyOutcome(
          OLD_HASH, True)]
  seen_statuses, seen_reasons = set(), set()
  for eligible, ready, route, callers, dns, outcome, syncing in itertools.product(
          (True, False, None), (True, False, None), every_route(), tuple(caller_routes()), (True, False, None), outcomes,
          (None, QUEUED)):
    given = DomainFacts(NOW_HASH, eligible, ready, (WorkerRoute("w1", NOW_HASH, route, ready), *callers), dns, outcome,
                        syncing)
    view = decide_domain(given)
    seen_statuses.add(view.status)
    seen_reasons.add(view.reason)
    current = outcome if outcome is not None and outcome.fingerprint == NOW_HASH else None

    assert view.status in STATUSES and view.reason in REASONS, given
    assert view.conforms == expected_conformance(given), given
    assert (view.status == "syncing") == (syncing is not None), given
    if view.status == "synced":
      assert view.conforms is True or (current is not None and current.succeeded and view.conforms is None), given
    if view.status == "error":
      assert current is not None and not current.succeeded and view.conforms is not True, given
    if view.conforms is True:
      assert view.status in ("syncing", "synced"), given
    if eligible is None:
      assert view.conforms is not True, given
    without_old = decide_domain(DomainFacts(NOW_HASH, eligible, ready, given.routes, dns, current, syncing))
    assert without_old == view, given

  assert seen_statuses == set(STATUSES)
  assert seen_reasons == REASONS
  assert set(ROUTES) == {route.kind for route in every_route()}


def header(domain_id, fingerprint):
  return f"# sage-domain {domain_id} {fingerprint}"


def test_the_listing_reads_each_routing_files_own_header():
  other = "1111111111111111"
  lines = [
      f"/home/sage/traefik/dynamic/{domain_artifact_name('file', ID)}:{header(ID, NOW_HASH)}",
      f"/home/sage/traefik/dynamic/{domain_artifact_name('file', other)}:{header(ID, NOW_HASH)}",
      f"/home/sage/traefik/dynamic/{domain_artifact_name('file', '2222222222222222')}:http:",
      "/home/sage/traefik/dynamic/_discovery-public-shop.yml:http:",
      "/home/sage/traefik/dynamic/domain-42.yml:# sage-domain 42 x",
  ]
  assert parse_route_listing(lines) == {ID: NOW_HASH, other: None, "2222222222222222": None}


def test_the_listing_command_reads_a_real_dynamic_directory(tmp_path):
  (tmp_path / domain_artifact_name("file", ID)).write_text(f"{header(ID, NOW_HASH)}\nhttp:\n  routers: {{}}\n")
  (tmp_path / domain_artifact_name("file", "1111111111111111")).write_text("")
  (tmp_path / "_discovery-public-shop.yml").write_text("http:\n")

  output = subprocess.run(["sh", "-c", routes_command(str(tmp_path))], capture_output=True, text=True, check=True).stdout

  assert parse_route_listing(output.splitlines()) == {ID: NOW_HASH}


def test_the_listing_command_lists_nothing_in_an_empty_directory(tmp_path):
  result = subprocess.run(["sh", "-c", routes_command(str(tmp_path))], capture_output=True, text=True)
  assert (result.returncode, result.stdout) == (0, "")


@pytest.mark.parametrize("template", sorted(set(ROUTING_TEMPLATES.values())))
def test_every_routing_template_starts_with_the_header_the_listing_reads(template):
  first = (TEMPLATES / template).read_text().splitlines()[0]
  rendered = re.sub(r"\$\{(\w+)\}", lambda m: {"ID": ID, "FINGERPRINT": NOW_HASH}[m[1]], first)
  assert parse_route_listing([f"/x/{domain_artifact_name('file', ID)}:{rendered}"]) == {ID: NOW_HASH}
