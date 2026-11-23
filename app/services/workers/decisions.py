"""A worker's status, decided from the facts its owners hold: whether its SSH server answers, whether its tunnel is
connected, how its setup went, what its newest report says, which of its tasks are queued or running, and whether its
removal was asked for."""
import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime

import config
from services.containers.placement import runtime_name
from services.domains import discovery
from services.domains.config import domain_artifact_owner
from services.worker_reports import Received, is_current
from services.workers.rendering import PRIVATE_RANGES, TRAEFIK_CONTAINER, TRAEFIK_FOLDERS
from utils.common import OPAQUE_ID

STATUSES = ("removing", "adding", "rebooting", "checking", "offline", "not ready", "online")
SSH = ("unobserved", "reachable", "unreachable", "unknown")
# Cloudflare's tunnel statuses, with None while it is not known.
TUNNELS = (None, "healthy", "degraded", "down", "inactive")
SETUPS = (None, "succeeded", "failed", "refused")
TASKS = {"remove_worker": "removing", "add_worker": "adding", "reboot_worker": "rebooting"}


@dataclass(frozen=True)
class WorkerReport:
  """How much is known of the worker from its reports, and what the newest says of its stack and of Traefik: the
  services of its stack that do not run, each service's status (`missing` when it is not listed), and how many
  containers it lists."""
  knowledge: str
  traefik_reachable: bool | None = None
  stack_down: tuple[str, ...] = ()
  stack: tuple[tuple[str, str], ...] = ()
  containers: int | None = None


@dataclass(frozen=True)
class WorkerFacts:
  """`ssh` is unknown while the Manager itself is cut off from the tailnet; `setup` is None until a setup has run
  since the Manager started, and refused when another Sage owns the worker; `tunnel` is its tunnel's status, None while
  it is not known; `removing` is its row's removal mark."""
  ssh: str
  setup: str | None
  report: WorkerReport
  tasks: frozenset[str] = frozenset()
  seconds_since_start: float = 0
  tunnel: str | None = None
  removing: bool = False


@dataclass(frozen=True)
class WorkerView:
  status: str
  reason: str | None = None
  detail: str | None = None

  @property
  def ready(self) -> bool | None:
    """Whether its containers can be reached; None while that is not known yet."""
    return None if self.status == "checking" else self.status == "online"


def report_of(received: Received | None, now: datetime) -> WorkerReport:
  if received is None:
    return WorkerReport("unobserved")
  snapshot = received.snapshot
  stack = tuple((name, snapshot.containers[name].status if name in snapshot.containers else "missing")
                for name in config.WORKER_STACK_SERVICES)
  return WorkerReport("known" if is_current(received.arrived_at, now) else "stale", snapshot.traefik_reachable,
                      tuple(name for name, status in stack if status != "running"), stack, len(snapshot.containers))


def connectivity(ssh: str, tunnel: str | None) -> str:
  """How the worker answers: reachable or unobserved as its SSH server says; offline when neither the tailnet nor its
  tunnel answers, or its SSH server does not answer and its tunnel is not known; tailnet_unreachable when only its
  tunnel is connected; unknown while the Manager is cut off from the tailnet and its tunnel is not known to be down."""
  if ssh in ("reachable", "unobserved"):
    return ssh
  if ssh == "unreachable":
    return "tailnet_unreachable" if tunnel in ("healthy", "degraded") else "offline"
  return "offline" if tunnel in ("down", "inactive") else "unknown"


def decide_worker(facts: WorkerFacts) -> WorkerView:
  """The status, in precedence order: its removal, a task under way, how it answers (`connectivity`), how its setup
  went, then what its reports say. A worker that has not reported yet is checking during REPORT_STARTUP_GRACE_SECONDS after the Manager
  starts and not ready after it: the grace never makes it ready."""
  if facts.removing:
    return WorkerView("removing")
  task = next((TASKS[name] for name in TASKS if name in facts.tasks), None)
  if task is not None:
    return WorkerView(task)
  answers = connectivity(facts.ssh, facts.tunnel)
  if answers == "offline":
    return WorkerView("offline", "ssh_unreachable" if facts.tunnel is None else "unreachable")
  if answers == "tailnet_unreachable":
    return WorkerView("not ready", "tailnet_unreachable")
  if answers == "unknown":
    return WorkerView("checking", "manager_offline")
  if answers == "unobserved" or facts.setup is None:
    return WorkerView("checking", "unobserved")
  if facts.setup == "refused":
    return WorkerView("not ready", "owned_elsewhere")
  if facts.setup == "failed":
    return WorkerView("not ready", "setup_failed")
  report = facts.report
  if report.knowledge == "unobserved":
    if facts.seconds_since_start < config.REPORT_STARTUP_GRACE_SECONDS:
      return WorkerView("checking", "unobserved")
    return WorkerView("not ready", "not_reporting")
  if report.knowledge == "stale":
    return WorkerView("not ready", "not_reporting")
  if report.stack_down:
    return WorkerView("not ready", "stack_down", ", ".join(report.stack_down))
  if report.traefik_reachable is not True:
    return WorkerView("not ready", "traefik_down" if report.traefik_reachable is False else "traefik_unchecked")
  return WorkerView("online")


def workable(ssh: str, setup: str | None, removing: bool = False) -> bool:
  """Whether Sage can act on the worker: its SSH server answers, it was set up since the Manager started and its
  removal was not asked for."""
  return ssh == "reachable" and setup == "succeeded" and not removing


def addressable(ssh: str, address: str | None) -> bool:
  """Whether the worker answers at an address in the tailnet."""
  return ssh == "reachable" and address is not None


def discovery_member(ssh: str, tunnel: str | None, setup: str | None, address: str | None,
                     seconds_since_start: float, removing: bool = False) -> bool | None:
  """Whether `discovery.int.<domain>` points at the worker: yes while it is reachable, set up since the Manager started
  and has an address; no once its removal was asked for, it is offline (`connectivity`) or its setup failed or was
  refused. None while that is
  left as it is: while the Manager is cut off from the tailnet or only the worker's tunnel answers, and otherwise (not
  observed, or not set up since the start) for UNKNOWN_HOLD_SECONDS after the Manager started, then no."""
  answers = connectivity(ssh, tunnel)
  if removing or answers == "offline" or setup in ("failed", "refused"):
    return False
  if workable(ssh, setup) and address:
    return True
  return None if answers in ("unknown", "tailnet_unreachable") or seconds_since_start < config.UNKNOWN_HOLD_SECONDS else False


def reboot_outcome(baseline_boot_id: str, boot_id: str | None, ready: bool | None) -> str:
  """What a requested reboot has achieved: verified once the worker runs a new boot and is ready; old_boot while it
  still runs the one from before the request; not_ready on a new boot that is not ready; unknown while the boot
  could not be read."""
  if boot_id is None:
    return "unknown"
  if boot_id == baseline_boot_id:
    return "old_boot"
  return "verified" if ready is True else "not_ready"


def stray_traefik_files(listing: list[str]) -> list[str]:
  """The entries of a listing of `TRAEFIK_FOLDERS` (`rendering.traefik_files_command`) in none of the forms this
  version writes there: the stack's `config.yml`, the certificates' `certs.yml` and `managed` folder, the Domains'
  routing files and the discovery files. What an older version left, removed when the stack is written."""
  dynamic, certs = TRAEFIK_FOLDERS

  def stray(path: str) -> bool:
    folder, _, name = path.rpartition("/")
    if folder == certs:
      return name != "managed"
    return folder == dynamic and not (name in ("config.yml", "certs.yml") or domain_artifact_owner("file", name)
                                      or (name.startswith(discovery.PREFIX) and name.endswith(discovery.SUFFIX)))
  return sorted(path for path in (line.strip() for line in listing) if stray(path))


def edge_subnets(listing: list[str]) -> tuple[str, ...]:
  """The edge network's subnets from `rendering.edge_network_command`'s listing, in order; any other line (Docker's
  warnings) is left out. Raises when there is none."""
  subnets = []
  for line in listing:
    try:
      subnets.append(str(ipaddress.ip_network(line.strip())))
    except ValueError:
      continue
  if not subnets:
    raise ValueError("Docker listed no subnet for the edge network.")
  return tuple(subnets)


@dataclass(frozen=True)
class AppNetwork:
  """An application's network on a worker as Docker lists it: its name, its container's id, its subnets and the
  containers on it."""
  name: str
  container_id: str
  subnets: tuple[str, ...]
  members: frozenset[str]


def parse_networks(listing: list[str]) -> tuple[AppNetwork, ...]:
  """The applications' networks from a listing of a line per network: its name, its container's id, its subnets and
  its members, each list comma-separated. A line naming anything but Sage's network of its container, or a subnet
  that isn't one, is left out."""
  networks = []
  for line in listing:
    fields = line.split(" ", 3)
    if len(fields) < 3 or not re.fullmatch(OPAQUE_ID, fields[1]) or fields[0] != runtime_name(fields[1]):
      continue
    try:
      subnets = tuple(str(ipaddress.ip_network(subnet)) for subnet in fields[2].split(",") if subnet)
    except ValueError:
      continue
    members = frozenset(member for member in (fields[3] if len(fields) > 3 else "").strip().split(",") if member)
    networks.append(AppNetwork(fields[0], fields[1], subnets, members))
  return tuple(networks)


def traefik_network_changes(networks: tuple[AppNetwork, ...], containers: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
  """The applications' networks to attach Traefik to and those to remove, given the ids of the worker's containers in
  the database: Traefik joins the network of each of those containers, and a network of a container gone that only
  Traefik is on goes."""
  attach, remove = [], []
  for network in networks:
    if network.container_id in containers:
      if TRAEFIK_CONTAINER not in network.members:
        attach.append(network.name)
    elif network.members <= {TRAEFIK_CONTAINER}:
      remove.append(network.name)
  return tuple(attach), tuple(remove)


def parse_aliases(lines: list[str]) -> dict[str, frozenset[str]]:
  """Traefik's aliases on each of its networks, from a line per network: its name, then its aliases comma-separated."""
  aliases = {}
  for line in lines:
    network, _, names = line.strip().partition(" ")
    if network:
      aliases[network] = frozenset(name for name in names.split(",") if name)
  return aliases


def alias_changes(networks: tuple[AppNetwork, ...], wanted: dict[str, tuple[str, ...]],
                  current: dict[str, frozenset[str]]) -> tuple[str, ...]:
  """The applications' networks Traefik is on with other aliases than `wanted`, to attach again with those."""
  return tuple(network.name for network in networks if TRAEFIK_CONTAINER in network.members and network.name in wanted
               and frozenset(wanted[network.name]) != current.get(network.name, frozenset()))


def dns_exceptions(resolv_conf: list[str]) -> tuple[str, ...]:
  """The resolvers of the `# ExtServers:` line Docker writes in a container's `resolv.conf` that the firewall lets DNS
  reach: those in `rendering.PRIVATE_RANGES`, in order and once each. A `host(…)` entry needs none, since Docker
  forwards it from the host's own network. Raises when the line is missing or names anything but addresses."""
  for line in resolv_conf:
    if not line.startswith("# ExtServers:"):
      continue
    entries = line.removeprefix("# ExtServers:").strip()
    if not (entries.startswith("[") and entries.endswith("]")):
      raise ValueError(f"Docker's resolvers are not a list: {line}")
    private, found = [ipaddress.ip_network(network) for network in PRIVATE_RANGES], []
    for entry in entries[1:-1].split():
      if entry.startswith("host(") and entry.endswith(")"):
        continue
      address = ipaddress.ip_address(entry)
      if any(address in network for network in private) and str(address) not in found:
        found.append(str(address))
    return tuple(found)
  raise ValueError("Docker recorded no resolvers in the container's resolv.conf.")


def worker_owner(revisions: dict) -> tuple[str, str] | None:
  """The Sage the worker belongs to, (instance id, domain) from its owner stamp; None when it has none."""
  instance_id, _, domain = revisions.get("owner", "").partition(" ")
  return (instance_id, domain) if instance_id else None


def worker_claim_refusal(revisions: dict, instance_id: str) -> str | None:
  """Why this Sage may not set the worker up: its owner stamp names another instance. None when it is unowned or this
  Sage's."""
  owner = worker_owner(revisions)
  if owner and owner[0] != instance_id:
    return f"it is managed by another Sage ({owner[1] or 'unknown domain'}); remove it from there first"
  return None
