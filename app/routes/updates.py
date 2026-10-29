import socketio

import config
from routes.applications import load_application
from routes.backups import get_platform_backup_status, list_platform_backups
from routes.info import get as info
from routes.info import get_release
from routes.tasks import list_tasks
from routes.workers import get as list_workers
from routes.workers import load_worker
from services.updates import Feed, Topic, UpdateFeed, Updates, UpdateSource, UpdateTopic

TOPICS = {
    UpdateTopic.INFO: Topic(info, frozenset({UpdateSource.QUEUE_STATE, UpdateSource.RESTORING, UpdateSource.SETTINGS_APPLYING,
                                             UpdateSource.CHECKS, UpdateSource.LATEST_VERSION})),
    UpdateTopic.TASKS: Topic(list_tasks, frozenset({UpdateSource.TASKS})),
    UpdateTopic.BACKUP_STATUS: Topic(get_platform_backup_status, frozenset({UpdateSource.BACKUP_STATUS})),
    UpdateTopic.BACKUPS: Topic(list_platform_backups, frozenset({UpdateSource.BACKUPS})),
    UpdateTopic.RELEASE: Topic(get_release, frozenset({UpdateSource.RELEASE})),
    UpdateTopic.WORKERS: Topic(list_workers, frozenset({UpdateSource.WORKER_LIST})),
    # One worker, followed by hostname, and one application, followed by `<project>/<name>`: the sources are keyed by it.
    UpdateTopic.WORKER: Topic(load_worker, frozenset({UpdateSource.WORKER_ENTRY, UpdateSource.WORKER_CONTAINERS}), parameterised=True),
    UpdateTopic.APPLICATION: Topic(load_application, frozenset({UpdateSource.APPLICATION}), parameterised=True),
    UpdateFeed.NOTIFICATIONS: Feed(),
    UpdateFeed.TASK_HISTORY: Feed(),
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
  Updates().connect(sid, lambda event, payload: sio.emit(event, payload, to=sid))


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
