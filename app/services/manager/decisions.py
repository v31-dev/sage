"""The decisions of the worker sync and the status sync, as plain functions of the state they are
given. The async tasks gather that state, call these, and apply the answer."""
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable

import config
from services.worker_reports import Received, Snapshot, container_started_at, is_current

# What a look at a worker's containers found for one application.
SEEN = "seen"          # the container is there, in `state`
ABSENT = "absent"      # the worker answered and has no such container running
UNKNOWN = "unknown"    # the worker could not be asked: nothing is known
OFFLINE = "offline"    # the worker is out of the pool


@dataclass(frozen=True)
class Observation:
  kind: str
  state: str | None = None
  deployed_label: str | None = None


@dataclass(frozen=True)
class Reconcile:
  """What to do with a container's status. `status` is the new one, None to leave it; with
  `stop_stale` the container is stopped first and `status` is the result of that succeeding."""
  status: str | None = None
  stop_stale: bool = False
  message: str | None = None
  level: str = "info"


def reconcile_container(name: str, status: str, observation: Observation, deploy_stamp: str | None) -> Reconcile:
  """The new status of a container that is not busy, from what was seen of it.

  A container that could not be looked at keeps its status: not knowing is not an error."""
  if observation.kind == UNKNOWN:
    return Reconcile()

  def not_running(why: str) -> Reconcile:
    if status != "active":
      return Reconcile()
    return Reconcile("error", message=f"Application container {name} is in error state ({why}).", level="error")

  if observation.kind == OFFLINE:
    return not_running("worker offline")
  if observation.kind == ABSENT:
    return not_running("not running")

  state = observation.state
  if state == "running":
    if observation.deployed_label != deploy_stamp:
      return Reconcile(
          "inactive", stop_stale=True, level="error",
          message=f"Application container {name} was running a stale version and has been stopped; "
          "redeploy the application to update it.")
    if status != "active":
      return Reconcile("active", message=f"Application container {name} is active again.", level="success")
    return Reconcile()
  if state in ("paused", "restarting"):
    if status != "error":
      return Reconcile("error", message=f"Application container {name} is in error state ({state}).", level="error")
    return Reconcile()
  return not_running(str(state))


def rollup_status(statuses: Iterable[str]) -> str | None:
  """The application's status from its containers': error if any is, active or inactive if all are,
  otherwise None (leave it)."""
  statuses = list(statuses)
  if not statuses:
    return None
  if "error" in statuses:
    return "error"
  if all(s == "active" for s in statuses):
    return "active"
  if all(s == "inactive" for s in statuses):
    return "inactive"
  return None


@dataclass(frozen=True)
class WorkerRow:
  hostname: str
  ip: str
  online: bool


@dataclass(frozen=True)
class WorkerAction:
  """`kind` is "offline" (take it out of the pool) or "setup" (converge it, from scratch when `force`)."""
  kind: str
  worker: WorkerRow
  reason: str
  force: bool = False


def plan_worker_sync(known: list[WorkerRow], peers: list[WorkerRow], force: bool, check_stamps: bool) -> list[WorkerAction]:
  """What the worker sync does, from the workers in the database and the tailnet's machines among
  them (`peers`, with `online` as the pings found them). A machine that is not a known worker is
  left alone: workers are added by hand. A worker that does not answer is never set up."""
  known_by_name = {row.hostname: row for row in known}
  actions = []

  for peer in peers:
    row = known_by_name[peer.hostname]
    if row.online and not peer.online:
      actions.append(WorkerAction("offline", peer, "went offline"))
    elif not row.online and peer.online:
      actions.append(WorkerAction("setup", peer, "came back online"))
    elif peer.online and row.ip != peer.ip:
      actions.append(WorkerAction("setup", peer, f"IP changed from {row.ip} to {peer.ip}"))
    elif peer.online and force:
      actions.append(WorkerAction("setup", peer, "force sync", force=True))
    elif peer.online and check_stamps:
      actions.append(WorkerAction("setup", peer, "stamp check"))

  listed = {peer.hostname for peer in peers}
  for row in known:
    if row.hostname not in listed and row.online:
      actions.append(WorkerAction("offline", row, "not found in the tailnet"))
  return actions


def worker_owner(revisions: dict) -> tuple[str, str] | None:
  """The Sage that a worker belongs to, as (instance id, domain) from its owner stamp, or None when it
  has none."""
  instance_id, _, domain = revisions.get("owner", "").partition(" ")
  return (instance_id, domain) if instance_id else None


def worker_claim_refusal(revisions: dict, instance_id: str) -> str | None:
  """Why this Sage may not set a worker up: its owner stamp names another instance. None when it is
  unowned or this Sage's."""
  owner = worker_owner(revisions)
  if owner and owner[0] != instance_id:
    return f"it is managed by another Sage ({owner[1] or 'unknown domain'}); remove it from there first"
  return None


def worker_stack_problem(snapshot: Snapshot | None, silent_for: float) -> str | None:
  """What a worker's report says is wrong with its stack, None when nothing is. `snapshot` is the
  current one, or None when the worker has no current report, `silent_for` the seconds since it
  last reported."""
  if snapshot is None:
    return "not reporting" if silent_for > config.WORKER_STACK_SILENCE_SECONDS else None
  down = [name for name in config.WORKER_STACK_SERVICES
          if name not in snapshot.containers or snapshot.containers[name].status != "running"]
  if down:
    return f"{', '.join(down)} not running"
  if snapshot.traefik_reachable is False:
    return "Traefik is not answering"
  return None


@dataclass(frozen=True)
class WorkerReboot:
  """A reboot the manager asked for: when, and the start of Telegraf's container on the worker's clock
  before it (None when the worker had not reported)."""
  since: datetime
  started_at: int | None


def is_worker_rebooting(mark: WorkerReboot, now: datetime, started_at: int | None) -> bool:
  """Whether a reboot asked for at `mark.since` is still under way: until the worker reports again with a
  Telegraf that has started since the request (`started_at`, from its current report, is another than
  `mark.started_at`), or the limit. Reports go on until the host goes down, so a report with the same start
  is from before the reboot. Whether the rest of the stack is up yet is the worker's readiness, not the reboot's.
  Without a start from before the request there is nothing to compare with, and any report counts as back."""
  if (now - mark.since).total_seconds() > config.WORKER_REBOOT_LIMIT_SECONDS:
    return False
  return started_at is None or started_at == mark.started_at


# The tasks that make a worker adding, removing or rebooting while they are queued or running.
WORKER_TASKS = ("add_worker", "remove_worker", "reboot_worker")


@dataclass(frozen=True)
class WorkerInputs:
  """What the worker marker knows of one worker, each field written by the one owner of that fact. A record
  without an `ip` is not a worker, as the table has no row for it: a push can arrive before the row does."""
  ip: str | None = None                 # the row in the Worker table
  online: bool = False                  # at the last sync its SSH server answered and it was set up
  tasks: frozenset[str] = frozenset()   # the `WORKER_TASKS` queued or running for it
  reboot: WorkerReboot | None = None    # the last reboot asked of it
  report: Received | None = None        # the newest report of its Telegraf
  path: str | None = None               # how the manager reaches it


def _current_report(worker: WorkerInputs, now: datetime) -> Received | None:
  return worker.report if worker.report is not None and is_current(worker.report.arrived_at, now) else None


def worker_ready(worker: WorkerInputs, now: datetime, started: datetime) -> bool:
  """Whether the worker is reporting and Traefik answers on it. A worker that has not reported yet is given
  REPORT_STARTUP_GRACE_SECONDS after `started`, when the manager started."""
  if worker.report is None:
    return (now - started).total_seconds() < config.REPORT_STARTUP_GRACE_SECONDS
  current = _current_report(worker, now)
  return current is not None and current.snapshot.traefik_reachable is not False


def worker_status(worker: WorkerInputs, now: datetime, started: datetime) -> str:
  """What the worker is shown as: "removing" while its removal is queued or running, "adding" while its
  addition is, "rebooting" from the request until it reports again after the reboot (`is_worker_rebooting`),
  "offline" when its SSH server did not answer (`online`), "not ready" when it is not ready (`worker_ready`),
  otherwise "online"."""
  if "remove_worker" in worker.tasks:
    return "removing"
  if "add_worker" in worker.tasks:
    return "adding"
  started_at = container_started_at(_current_report(worker, now), "telegraf")
  if "reboot_worker" in worker.tasks or (worker.reboot is not None and is_worker_rebooting(worker.reboot, now, started_at)):
    return "rebooting"
  if not worker.online:
    return "offline"
  return "online" if worker_ready(worker, now, started) else "not ready"


def worker_entry(hostname: str, worker: WorkerInputs, now: datetime, started: datetime) -> dict:
  """The worker as the worker list shows it. `container_count` is the containers of its current report."""
  current = _current_report(worker, now)
  return {
      "hostname": hostname,
      "ip": worker.ip,
      "online": worker.online,
      "status": worker_status(worker, now, started),
      "path": worker.path,
      "container_count": len(current.snapshot.containers) if current else None,
  }


def worker_with_row(worker: WorkerInputs, ip: str, path: str | None) -> WorkerInputs:
  """The record of a worker whose row has this `ip`. One that had no row yet starts not online and not rebooting,
  whatever an earlier worker of the same name left."""
  if worker.ip is None:
    worker = replace(worker, online=False, reboot=None)
  return replace(worker, ip=ip, path=path)


def worker_without_row(worker: WorkerInputs) -> WorkerInputs:
  """The record of a worker whose row is gone: what the queue says about its tasks is all that stays."""
  return WorkerInputs(tasks=worker.tasks)
