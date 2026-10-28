import socketio

import config
from routes.info import get as info
from services.updates import CHECKS, LATEST_VERSION, NOTIFICATIONS, QUEUE_STATUS, WORKERS, Feed, Topic, Updates

TOPICS = {
    "info": Topic(info, frozenset({QUEUE_STATUS, WORKERS, CHECKS, LATEST_VERSION})),
    NOTIFICATIONS: Feed(),
}

SCHEMES = {"http": "http", "https": "https", "ws": "http", "wss": "https"}


def same_origin(origin: str, environ: dict) -> bool:
  """Whether the origin of a page is the manager's own: the scheme the request arrived with (a WebSocket's `ws` and
  `wss` are the page's `http` and `https`) and the host it was sent to."""
  scheme = SCHEMES.get(environ["asgi.scope"]["scheme"])
  return scheme is not None and origin == f"{scheme}://{environ.get('HTTP_HOST', '')}"


sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=same_origin,
    ping_interval=config.UPDATES_PING_INTERVAL_SECONDS,
    ping_timeout=config.UPDATES_PING_TIMEOUT_SECONDS,
)


@sio.event
async def connect(sid, environ, auth=None):
  Updates().connect(sid, lambda event, payload: sio.emit(event, payload, to=sid), TOPICS)


@sio.event
async def disconnect(sid, reason=None):
  Updates().disconnect(sid)


@sio.on("subscribe")
async def subscribe(sid, topic=None, *_):
  if isinstance(topic, str):
    Updates().subscribe(sid, topic)


@sio.on("unsubscribe")
async def unsubscribe(sid, topic=None, *_):
  if isinstance(topic, str):
    Updates().unsubscribe(sid, topic)
