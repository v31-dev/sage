"""Each placement's record on its worker, as the Manager last read or wrote it."""
import config
from services.applications.updates import container_changed
from services.base import Base
from services.containers.placement import record_path, records_command
from services.containers.record import TargetRead, TargetRecord, encode_record, parse_record_listing
from services.db.models import Container
from services.tailscale import Tailscale


class Containers(Base):
  """The last read of each container's record, by container id, unobserved until the first. Only the Manager writes
  a record, under the container's scope, so once a write returns the worker holds what it wrote."""

  def __init__(self):
    super().__init__()
    self._reads: dict[str, TargetRead] = {}

  def target(self, container_id: str) -> TargetRead:
    with self.lock:
      return self._reads.get(container_id, TargetRead("unobserved"))

  async def read(self, container: Container) -> TargetRead:
    """The container's record on its worker: present, absent or invalid; unreachable, with the last record kept, when
    the worker cannot be asked."""
    try:
      _, lines = await Tailscale().exec_command(
          container.worker_id, records_command(config.WORKER_HOME_DIR, container.id), timeout=30)
    except Exception:
      read = TargetRead("unreachable", self.target(container.id).record)
    else:
      read = parse_record_listing(lines).get(container.id, TargetRead("absent"))
    self._hold(container, read)
    return read

  async def write(self, container: Container, record: TargetRecord):
    """Replaces the container's record whole; raises when it may not have been written."""
    await Tailscale().sync_file(container.worker_id, config.TEMPLATES_DIR / "worker/file",
                                record_path(config.WORKER_HOME_DIR, container.id), {"CONTENT": encode_record(record)})
    self._hold(container, TargetRead("present", record))

  def forget(self, container_id: str):
    """Drops the container's read once its row is gone."""
    with self.lock:
      self._reads.pop(container_id, None)

  def _hold(self, container: Container, read: TargetRead):
    with self.lock:
      changed = self._reads.get(container.id) != read
      self._reads[container.id] = read
    if changed:
      container_changed(container)
