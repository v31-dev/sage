"""A container's state as its pages show it, worked out from the facts its owners hold."""
from dataclasses import asdict, dataclass

from services.containers.decisions import ContainerFacts, ContainerView, Runtime, decide_container, routable
from services.containers.record import Authority, TargetRead
from services.containers.runtime import runtime_of
from services.containers.targets import Containers
from services.db.models import Container, ContainerEvent, Instance
from services.tailscale import Tailscale
from services.worker_reports import WorkerReports
from utils.common import utcnow


@dataclass(frozen=True)
class QueuedRequest:
  """A request waiting its turn on the container: its operation, its task, and whether Cancel takes it."""
  operation: str
  task_id: str
  cancellable: bool


@dataclass(frozen=True)
class ContainerState:
  """The decided view with the facts it was decided from: the record as last read, the runtime and the live
  operation's name; whether it can be routed to; and the requests queued on it, which leave the view as it is."""
  view: ContainerView
  target: TargetRead
  runtime: Runtime
  live: str | None
  routable: bool | None
  queued: tuple[QueuedRequest, ...] = ()


def container_state(container: Container, instance: Instance, live: str | None, queued: tuple[QueuedRequest, ...],
                    desired_hash: str | None) -> ContainerState:
  """The container's state now, given the operation running on it, the requests queued on it and the fingerprint of
  its application's current inputs."""
  target = Containers().target(container.id)
  reachable = {"reachable": True, "unreachable": False}.get(Tailscale().ssh(container.worker_id))
  runtime = runtime_of(WorkerReports().latest(container.worker_id), container.id, target.record, utcnow(), reachable)
  facts = ContainerFacts(Authority(instance.id, instance.state_generation, container.id), target, runtime, live, desired_hash)
  return ContainerState(decide_container(facts), target, runtime, live, routable(facts), queued)


def container_reference(container: Container, state: ContainerState) -> dict:
  """The container as a list of containers shows it: its worker, its application and its decided state."""
  application = container.application
  return {
      "id": container.id,
      "worker": {"hostname": container.worker.hostname, "subdomain": container.worker.subdomain},
      "application": {
          "name": application.name,
          "label": application.label,
          "project": {"name": application.project.name, "label": application.project.label},
      },
      "view": asdict(state.view),
  }


def container_payload(container: Container, state: ContainerState, domains: list[dict], events: list[ContainerEvent]) -> dict:
  """The container as its pages show it: its reference, the facts its state was decided from (its record as last read,
  its runtime, the operation running on it, whether it can be routed to), the requests queued on it in their turn, the
  payloads of its domains, and its events, newest first."""
  return {
      **container_reference(container, state),
      "target": asdict(state.target),
      "runtime": asdict(state.runtime),
      "live": state.live,
      "routable": state.routable,
      "queued": [asdict(request) for request in state.queued],
      "domains": domains,
      "events": [{"id": event.id, "action": event.action, "transition": event.transition, "operation_id": event.operation_id,
                  "reason": event.reason, "task_id": event.task_id, "part_id": event.part_id, "created_at": event.created_at}
                 for event in events],
      "created_at": container.created_at,
      "updated_at": container.updated_at,
  }
