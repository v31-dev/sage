"""A container's writes on its lifecycle: the intent of the request asked of it, which a restart resumes until the
request reaches its outcome."""
from services.db.models import Container
from services.db.writes import get, write
from utils.common import utcnow


def record_intent(container_id: str, intent: str):
  """Records `intent` as what was asked of the container."""
  with write():
    container = get(Container, Container.id == container_id)
    container.intent = intent
    container.save()


def settle_intent(container_id: str, intent: str):
  """Clears the container's intent while it is still `intent`; another request's intent, or a container gone, is left
  as it is."""
  with write():
    Container.update(intent=None, updated_at=utcnow()).where(
        (Container.id == container_id) & (Container.intent == intent)).execute()
