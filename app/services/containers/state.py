"""A container's state as its pages show it, worked out from the facts its owners hold."""
from dataclasses import dataclass

from services.containers.decisions import ContainerFacts, ContainerView, Runtime, decide_container
from services.containers.record import Authority, TargetRead
from services.containers.runtime import runtime_of
from services.containers.targets import Containers
from services.db.models import Container, Instance
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from utils.common import utcnow


@dataclass(frozen=True)
class ContainerState:
  """The decided view with the facts it was decided from: the record as last read, the runtime and the live
  operation's name."""
  view: ContainerView
  target: TargetRead
  runtime: Runtime
  live: str | None


def container_state(container: Container, instance: Instance, live: str | None, desired_hash: str | None) -> ContainerState:
  """The container's state now, given the operation running on it and the fingerprint of its application's current
  inputs."""
  target = Containers().target(container.id)
  reachable = {"reachable": True, "unreachable": False}.get(Tailscale().ssh(container.worker_id))
  runtime = runtime_of(WorkerReports().latest(container.worker_id), container.id, target.record, utcnow(), reachable)
  view = decide_container(ContainerFacts(Authority(instance.id, instance.state_generation, container.id), target, runtime,
                                         live, desired_hash))
  return ContainerState(view, target, runtime, live)
