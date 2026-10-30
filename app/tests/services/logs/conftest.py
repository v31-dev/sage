from types import SimpleNamespace

import pytest

from services.logs import Logs
from services.updates import Updates


@pytest.fixture
def logs(data_dir):
  """The service without its capture, which would write from another thread."""
  logs = Logs()
  yield logs
  for shard in logs._stores.values():
    shard.db.close()


@pytest.fixture
def published(register):
  """What the service publishes, as (feed, rows, key)."""
  sent = []
  register(Updates, SimpleNamespace(publish=lambda feed, rows, key=None: sent.append((feed, rows, key))))
  return sent
