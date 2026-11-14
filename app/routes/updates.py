import socketio

import config
from services.backups.payload import platform_backups
from services.manager import Manager
from services.releases import Releases
from services.updates import Feed, Topic, UpdateFeed, Updates, UpdateSource, UpdateTopic

# Each topic's loader is the reader its route returns, called when the topic is sent, so importing this table starts no
# owner.
TOPICS = {
    UpdateTopic.INFO: Topic(lambda: Manager().info(), frozenset({
        UpdateSource.QUEUE_STATE, UpdateSource.RESTORING, UpdateSource.SETTINGS_APPLYING, UpdateSource.SETTINGS_TESTING,
        UpdateSource.CHECKS, UpdateSource.CERTIFICATES, UpdateSource.TUNNELS, UpdateSource.LATEST_VERSION})),
    UpdateTopic.TASKS: Topic(lambda: Manager().task_snapshot(), frozenset({UpdateSource.TASKS})),
    UpdateTopic.BACKUP_STATUS: Topic(lambda: Manager().backup_status(), frozenset({UpdateSource.BACKUP_STATUS})),
    UpdateTopic.BACKUPS: Topic(platform_backups, frozenset({UpdateSource.BACKUPS})),
    UpdateTopic.RELEASE: Topic(lambda: Releases().latest(), frozenset({UpdateSource.RELEASE})),
    UpdateTopic.WORKERS: Topic(lambda: Manager().worker_list(), frozenset({UpdateSource.WORKER_LIST})),
    UpdateTopic.DOMAINS: Topic(lambda: Manager().domains_page(), frozenset({UpdateSource.DOMAINS})),
    # One worker, followed by hostname, and one application, followed by `<project>/<name>`: the sources are keyed by it.
    UpdateTopic.WORKER: Topic(lambda hostname: Manager().worker_page(hostname),
                              frozenset({UpdateSource.WORKER_ENTRY, UpdateSource.WORKER_CONTAINERS}), parameterised=True),
    UpdateTopic.APPLICATION: Topic(lambda name: Manager().application_page(*name.partition("/")[::2]),
                                   frozenset({UpdateSource.APPLICATION}), parameterised=True),
    UpdateFeed.NOTIFICATIONS: Feed(),
    UpdateFeed.TASK_HISTORY: Feed(),
    # A task's lines and an application's, followed by the task and by `<project>-<app>`.
    UpdateFeed.TASK_LOGS: Feed(parameterised=True),
    UpdateFeed.APPLICATION_LOGS: Feed(parameterised=True),
    UpdateFeed.REQUESTS: Feed(),
    # A settings section's Test outcomes, followed by section.
    UpdateFeed.SETTING_TESTS: Feed(parameterised=True),
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
