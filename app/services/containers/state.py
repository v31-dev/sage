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
class ContainerState:
  """The decided view with the facts it was decided from: the record as last read, the runtime and the live
  operation's name; and whether it can be routed to."""
  view: ContainerView
  target: TargetRead
  runtime: Runtime
  live: str | None
  routable: bool | None


def container_state(container: Container, instance: Instance, live: str | None, desired_hash: str | None) -> ContainerState:
  """The container's state now, given the operation running on it and the fingerprint of its application's current
  inputs."""
  target = Containers().target(container.id)
  reachable = {"reachable": True, "unreachable": False}.get(Tailscale().ssh(container.worker_id))
  runtime = runtime_of(WorkerReports().latest(container.worker_id), container.id, target.record, utcnow(), reachable)
  facts = ContainerFacts(Authority(instance.id, instance.state_generation, container.id), target, runtime, live, desired_hash)
  return ContainerState(decide_container(facts), target, runtime, live, routable(facts))


def container_payload(container: Container, state: ContainerState, domains: list[dict], events: list[ContainerEvent]) -> dict:
  """The container as its pages show it: its worker and application, its decided state with the facts it was decided
  from (its record as last read, its runtime, the operation running on it, whether it can be routed to), the payloads of
  its domains, and its events, newest first."""
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
      "target": asdict(state.target),
      "runtime": asdict(state.runtime),
      "live": state.live,
      "routable": state.routable,
      "domains": domains,
      "events": [{"id": event.id, "action": event.action, "transition": event.transition, "operation_id": event.operation_id,
                  "reason": event.reason, "task_id": event.task_id, "part_id": event.part_id, "created_at": event.created_at}
                 for event in events],
      "created_at": container.created_at,
      "updated_at": container.updated_at,
  }
