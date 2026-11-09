import pytest

from services.updates import Updates


class Signals:
  def __init__(self):
    self.signals = []

  def changed(self, source, key=None):
    self.signals.append((source, key))


@pytest.fixture
def signals(register):
  """What was published, as (source, key) pairs in order."""
  return register(Updates, Signals()).signals
