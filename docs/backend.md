# Backend

## Stack

- Python 3.14
- FastAPI
- APScheduler + croniter
- Peewee + SQLite
- httpx (also the Cloudflare API, called directly)
- Tailscale CLI
- boto3 (sync, offloaded to the task's pool)

## Configuration

`app/config.py` holds the locations (`DATA_DIR`, `DB_PATH`, `STATIC_DIR`, the worker home
directory), external endpoints and timings (retry delays, certificate waits, link expiries,
retention). A module reads a value as `config.NAME` where it is used, not `from config import
NAME`, so a test can override one attribute. The required env vars are read with `get_env`.

The main database is deferred: `Database` opens it at `config.DB_PATH` at startup, so importing
the models touches no disk.

Stored-value codecs live in `services/db/fields.py`; pure name, origin and multiline key/value rules
live in `values/`. Importing these modules needs no runtime configuration and loads no database models
or signals. Bootstrap explicitly calls `initialize_encryption` from `services/db/encryption.py` before
encrypted storage access. JSON fields share serialization, encrypted fields share Fernet conversion,
and `EncryptedKeyValueField` validates text without rewriting comments or quoting. `ChoiceField`
derives Python write validation and a SQLite `CHECK` from the same allowed values.

## API Surface

The main API is mounted in `app/api.py` and exposes route groups for:

- `info`
- `domains`
- `backups`
- `settings`
- `projects`
- `workers`
- `notifications`
- `tasks`
- `updates`, the Socket.IO server at `/api/socket.io/` (see Pushed Updates)

Nested routes cover:

- applications
- containers
- volumes
- volume backups

The ingestion API is separate in `app/api_ingest.py` and serves what the workers push on port 9001: Telegraf's reports at `/api/telegraf/report` (kept in memory by `services/worker_reports.py`, read by the operation queue; a report from a hostname that is not a configured worker is refused with a 404 and logged once) and Vector's logs at `/api/vector/logs`: Traefik's and the applications' lines (the worker's Vector ships Traefik and the containers with the `sage.container` label, each line naming its application from `sage.application`, and sends each event with only the container's name, the time, the stream and the message, in batches of at most 4 MB). The intake refuses a push over `LOG_INTAKE_MAX_BYTES` with a 413, and decodes and writes it on `LOGS_EXECUTOR` (`Logs.ingest`), off the event loop, one write per file: Traefik's access lines go to the requests in columns and its own lines are dropped, each application's lines to its shard. A line that is not an event is skipped and counted in a warning. A write that fails answers 503, which Vector sends again after; a file already written then gets that batch twice.

## Pushed Updates

`services/updates.py` (`Updates`) pushes state to the UI's Socket.IO connections, and `routes/updates.py` holds the Socket.IO server (`sio`). A topic is a resource: a connection that follows it is sent its whole state each time it changes, as the JSON of the REST route for it. Subscribing sends nothing; a page loads the first copy from the route. A feed is a list that grows: a connection that follows it is sent each row that is published to it.

- **Transport.** Socket.IO at `/api/socket.io/` (`python-socketio` and `python-engineio`, pinned in `requirements.txt`). The browser starts with long-polling and upgrades to a WebSocket, which uvicorn serves through `websockets` (also pinned); reconnecting, and falling back to long-polling, are the libraries'. `api.build_asgi_app` gives the hub the `TOPICS` table (`Updates().serve`) and puts the Socket.IO server in front of the FastAPI app, so the API's refusal of changes while paused (an HTTP middleware) does not apply to it, and in production wraps both in `OnlyManagerAddress`.
- **Events.** Client to server: `subscribe` and `unsubscribe`, with a topic name. `subscribe` is acknowledged once the connection is subscribed, which is the point from which every change reaches it. Server to client: `state` `{topic, data}` (for a feed, `data` is a list of rows, one list per published batch) and `topic_error` `{topic, status}` for an unknown topic (404) and for a followed topic whose route answers with an error instead of its state (a 404 for a worker or an application that is gone; the hub sends it in place of a state and does not log it). A message that is not a topic name is ignored.
- **Topics** are the `TOPICS` table in `routes/updates.py`: a loader (the REST function, so a route and its topic cannot differ) and the sources whose change makes it worth computing again. The names are the `UpdateTopic` enum (state topics) and the `UpdateFeed` enum (feeds), and the sources are `UpdateSource` (all in `services/updates.py`, each value unique), so a name cannot be reused by accident and `publish` takes only a feed. `info` (`GET /api`) follows `queue_state`, `restoring`, `settings_applying`, `checks`, `certificates` and `latest_version`; `workers` (`GET /workers`, each worker's payload) follows `worker_list`; `domains` (`GET /domains/`, every Domain's payload and every container one can be linked to) follows `domains`; `tasks` (`GET /tasks`, the running and queued tasks) follows `tasks`; `backup-status` (`GET /backups/platform_backup_status`) follows `backup_status`; `backups` (`GET /backups`, the platform backups) follows `backups`; `release` (`GET /info/release`) follows `release`. Two topics take an argument and are followed as `name:argument` (`Topic(parameterised=True)`, whose loader is given the argument; a name with an argument that the topic does not take, or without one that it needs, is a 404 like an unknown topic, and a name is forgotten when its last follower leaves): `worker:<hostname>` (`GET /workers/{hostname}`: the worker's payload with its containers' payloads and why it can't be removed now, `removal_refusal`) follows `worker_entry` and `worker_containers`; `application:<project>/<name>` (the application route's payload) follows `application`. Their sources are keyed: `Updates().changed(source, key)` sends only the topic whose argument is that key, a keyed source never reaches a topic without an argument and an unkeyed one never reaches a topic with one. `notifications` and `task-history` are feeds and have no loader: the routes `GET /notifications` and `GET /tasks/history` (the newest `limit` finished tasks, at most `LIST_MAX_ROWS`, newest finished first, filtered by name, scope and its children (`*` for any characters, in any case), executor, status and a window the task was active in; with how many match, and the filters' choices at `GET /tasks/history/filters`) are where a page gets the rows it starts with and the ones it missed. Three more feeds carry the logs as they are written: `task-logs:<task>`, published by the drain thread one line at a time; `application-logs:<project>-<app>` (the shard's name) and `requests`, published by the intake, one batch per file and push. The first two are parameterised feeds (`Feed(parameterised=True)`, followed as `name:argument` and forgotten when the last follower leaves), and `publish(feed, rows, key)` sends a parameterised feed's rows to the followers of that key. A feed nobody follows is not even encoded: a view that follows it from then on loads what was stored before. Their routes take `after_id`, the last row a view holds, which is what a view loads each time the socket connects.
- **Sources** only say "I changed", with no detail, and their owner fires one only when what it owns differs from before, so the hub does not compare anything. `Updates().changed(source)` is safe from any thread (it hands the change to the event loop) and does nothing before the first connection. Several come from the queue, and each is declared by the owner of the data. A `Fact` (`services/updates.py`) is a value an owner works out from something that changes: `check()` works it out and, when it differs from the last time, calls its callback with the new value and then fires its source; the value is worked out and handed to the callback under a lock of its own, since the queue's hook runs on many threads, so the callback gets the values in the order they were worked out. `Manager.queue_facts` is the list of every owner's facts, put together in `Manager.__init__` from `tasks_queue_facts` (the queue: `tasks`, the snapshot of the running and queued tasks, and `queue_state`), `settings_queue_facts` (`settings_applying`, the task that makes each section apply), `platform_queue_facts` (`restoring`, the backup being restored, and `backup_status`, whether a platform backup runs and whether backups are being discovered), `containers_queue_facts` (the operations running on containers and the requests queued on them, which tell those containers' pages), `domains_queue_facts` (the applies queued or running, which tell those Domains' pages) and `workers_queue_facts` (the worker tasks queued or running, which tell those workers' pages). `Manager._queue_changed`, the `TaskQueue` hook (called outside its lock on any change of the running or queued tasks or the state), only checks each one; it knows no owner. The others: `worker_list`, from the worker list (below); `worker_entry`, from the worker list, once for each worker whose entry differs; `worker_containers` and `application`, from the code that writes the rows (below); `checks`, for each settings check (it records a new `checked_at`); `latest_version`, when `refresh_latest_release` finds a version that differs; and `release`, whenever a release was fetched (its `fetched_at` is new). A new topic is a loader and its sources; what a topic shows that no source covers stays stale until the next change, so a source is tested to fire when its owner's state changes and not when it does not.
- **The workers.** Each worker's state is worked out when asked (`Manager().worker_states()`, `services/workers/state.py`) from the facts their owners hold: its SSH server's answer and its address in the tailnet (`Tailscale.ssh` and `Tailscale.address`, from the 30-second `observe_workers`), its setup outcome since the manager started (the `Workers` owner), its newest report (`WorkerReports`), the worker tasks queued or running for it and the start-up grace; `decide_worker` (`services/workers/decisions.py`) gives its status, reason and readiness. Pages are told through `worker_list` and `worker_entry`: a fact over every worker's state, checked at each observation, path refresh and report, tells only the workers whose state changed, and a queue fact tells a worker whose task is queued, starts or ends.
- **The rows of applications, containers and Domains** say so where they are written, with the key of what they changed: each resource command tells after its commit, through `services/applications/updates.py` and `services/domains/updates.py`. `application_changed` tells the application's page; `container_changed` the application's page, the page of the container's worker and the Domains page; `hosts_changed` the application's page and those of every worker it is on, for what a worker's page shows of an application (its label, its project's); `domain_changed` the Domains page, the pages of the applications and workers of its container and of the one it was linked to before, and those of its namespace worker now and before, whose removal its internal Domains refuse (`domains_changed` the Domains page alone, which a worker's new subdomain calls too). The `Containers` owner tells a container's pages when the record it holds changes, and the report intake those of the containers a report changed (`containers_reported`). `Domains` tells a Domain's pages when what it holds of the Domain changes, and queue changes tell the same topics for a container's running and queued operations and a Domain's `syncing`. At the end of a platform restore the UI reloads (`restoring`). Nothing hooks the database.
- **The rows of backups** say so where they are written: the Backup commands (`services/backups/commands.py`) fire `backups` for a row a volume or platform backup adds (`add_backup`), the rows discovery and retention add or remove (`apply_catalog`) and a row a deletion removes (`remove_backup`).
- **Feeds** are sent rows, not told of a change: `Updates().publish(feed, rows)` (rows in the REST route's shape, safe from any thread, nothing before the first connection) sends them to the feed's subscribers without reading anything. `Notifications().notify` publishes each stored notification, and `Manager._persist_task` each finished task once it is stored, as `model_to_dict` of the row, which is the shape the list returns. Each published batch is its own frame, in the order published, and a frame is never replaced by a later one. The UI gets the rows a page starts with, and the ones it missed while disconnected, from `GET /notifications` (see frontend.md), and merges by id.
- **What is sent.** After a change the hub waits `UPDATES_COALESCE_SECONDS` (3 s) so a burst is one pass, then computes each topic somebody follows that depends on a source that changed, on `UPDATES_EXECUTOR`, and sends it to its subscribers. A topic is computed one at a time, so an older state never overtakes a newer one, and a change that arrives during a computation is computed once more afterwards. A connection holds only the newest pending frame for each topic (the rows of a feed are the exception, none replaces another), so a slow browser skips states and never queues them, and one that cannot be written to does not hold up the others. A loader that fails is logged and nothing is sent; the topic is computed again at the next change.
- **Heartbeat.** The server pings each connection every `UPDATES_PING_INTERVAL_SECONDS` (10 s) and drops one that does not answer within `UPDATES_PING_TIMEOUT_SECONDS` (10 s). The UI shows each ping as the heartbeat of its indicator.
- **The handshake.** A browser lets any page connect to the manager and CORS does not stop it, so `same_origin` (in `routes/updates.py`) accepts a request only if its `Origin` is the scheme the request arrived with and the host it was sent to; a client that sends no `Origin` is not a page and is accepted. The scheme is the ASGI scope's, with a WebSocket's `ws` and `wss` read as `http` and `https`; uvicorn sets it from `X-Forwarded-Proto` when the proxy is trusted (127.0.0.1), and from the connection itself when it serves TLS directly. engine.io's own default rule, which builds the origin from `X-Forwarded-Proto` alone, refused every WebSocket handshake behind Traefik (it says `wss` for one, where a page's origin says `https`) and would have refused them in production too (no header, so `http`); a proxy in front of the manager has to say how it was reached. In production `OnlyManagerAddress` (pure ASGI, so it sees HTTP and WebSockets) answers only at the manager's address: another name is redirected, or for a WebSocket refused. This adds no authentication; it keeps the exposure of the read API as it was.
- **Access log.** Socket.IO's path (`SOCKET_IO_PATH`, `/api/socket.io`) is left out of the access log, since long-polling is a request every few seconds.
- **Tests.** `tests/routes/test_updates.py` serves the real app with uvicorn, over plain HTTP and with TLS of its own as in production, and connects with `socketio.Client` over long-polling, a WebSocket and the upgrade from one to the other, with the headers of a proxy and without (`websocket-client` is a test requirement).

## Orchestration Layer

`app/services/manager/` is a package of mixins composed into the `Manager` singleton, the workflows' coordinator:

- containers' lifecycle, recovery, resets and orphans (`containers.py`, `lifecycle.py`)
- volume backups and restores, and the backup catalog (`backups.py`, `backup_catalog.py`)
- Domain applies, the Domain pass and discovery (`domains.py`, `discovery.py`)
- workers' observation, setup, stack repair, add, removal and reboot (`workers.py`)
- certificate renewal and distribution (`certificates.py`)
- the platform backup and restore, what runs at startup, and the daily cleanup (`platform.py`)
- settings checks (`settings.py`) and the summary (`summary.py`)
- the queue's Manager-facing surface and its facts (`queue.py`)

Each resource's own rules, decisions and facts live in its package under `services/` (its commands, decisions, state
and rendering); the mixins call them.

Most high-level backend behavior ultimately flows through this service, and almost all of it is dispatched through the operation queue (below).

## Important Services

- `services/base.py`
  - thread-safe singleton base class with `RLock`
- `services/db/`
  - Peewee models and database bootstrap
- `services/settings.py`
  - persistent settings state for S3 and notifications; every field is optional and set from the UI
- `services/cloudflare.py`
  - Cloudflare DNS and tunnel management, configured from env (`DOMAIN`, `CLOUDFLARE_API_TOKEN`); finds the domain's active zone and, from it, the account that holds it; connects at startup and again on use if that failed. `sync_dns_records` makes the records with each comment exactly a given set, from one list of the zone (the workers' `*.<subdomain>.int` records, the public names, discovery's)
- `services/certs.py`
  - owns manager, shared discovery and hostname-keyed worker certificate material, exact SAN/key validation,
    ACME DNS-01 issuance, retirement/revocation, startup retry and the manager's TLS reload; memory-only
    inventory/status fires the certificate source when validity, names, expiry or errors change
- `services/tailscale.py`
  - Tailscale status, SSH command execution and SFTP file sync to workers (asyncssh over `tailscale nc`, since the manager's tailscaled runs in userspace mode)
- `services/manager/certificates.py`
  - the certificates' renewal and the setup of the manager's domain and certificate before serving (mixin)
- `services/metrics/` (`peaks.py`, `sampler.py`, `series.py`, `shards.py`)
  - metrics (workers' per-minute peaks from Telegraf's reports, written by the queue; the manager's own container in-process). A minute is written to its shard once it has ended; the queries add the minutes still in memory, so the latest reading is at most one report old and the `1m` view is the last two minutes
- `services/worker_reports.py`
  - Telegraf's reports: parsing a batch into snapshots, and the newest snapshot of each worker in memory
- `services/logs/`: the one `Logs()`, a mixin for each concept
  - `store.py`: the files of the logs directory, each opened once, their size limits, the retention and the orphan sweep, and the stored time (`stored_time`)
  - `applications.py`: each application's shard and its full-text search
  - `requests.py`: Traefik's access lines as requests in columns
  - `tasks.py`: the lines of the manager's queued tasks, captured in-process into the task logs
  - `intake.py`: a Vector push split between the requests and the applications
- `services/s3.py`
  - S3 validation, upload, delete, and presigned URL support
- `services/notification.py`
  - notices (`notify`: the Notification row, the `notifications` feed and the off-thread delivery), formatting and webhook send

## Logging And Task Context

The backend relies on `app/utils/logging.py` for task tracing.

Key rules:

- Every line logged inside a queued task carries its `task_id`; a request has none. Each container operation is a task of its own, and its `ContainerEvent`s carry the task's id.
- `task_id` lives in a `ContextVar`.
- The operation queue's `run_task` sets the `task_id` for each queued task; the offload helpers copy the context so worker threads keep it.
- Each line on stdout shows its task's id (`TaskIdFilter`).
- The access log is uvicorn's, without the requests asked every few seconds or more: a route marks itself, `@router.get(path, openapi_extra=NO_ACCESS_LOG)` (the health check, the log, metrics and task log loads, and the workers' pushes), and Socket.IO's path (`SOCKET_IO_PATH`) is left out too. `main.py` reads the marked paths from the two apps' OpenAPI description, which lists the routes of every router they include, when it sets up the logging (`unlogged_paths`, `setup_logger`). No server turns `access_log` off: the access logger is shared, so that would silence it for all of them.

This is a project-wide pattern, not a local implementation detail.

## Async And Blocking Work

- Local Peewee + SQLite calls run inline (WAL mode; cheap and local).
- Blocking network/remote I/O is offloaded onto a thread pool — never `asyncio.to_thread()`, which runs on the default executor, outside the lane pools. Two helpers in `app/utils/executor.py`:
  - `run_in_executor_with_context(func, *args, executor=None)` — awaited, from async code. Resolves the pool as explicit `executor` → the running task's lane (`active_executor`) → otherwise raises. Returns an awaitable.
  - `submit_with_context(executor, func, *args)` — fire-and-forget, works without a running loop (e.g. `Notifications.notify` webhook sends).
- Both `copy_context()` so the worker thread keeps the caller's `ContextVar`s — notably `task_id` for log correlation.

## Operation Queue

The Manager owns a single in-memory `TaskQueue` (`app/utils/queue.py`). It is the one place concurrency is controlled; the scheduler only enqueues.

- **Enqueue.** Every operation runs via `Manager().add_task(task=..., key=..., scopes=..., executor=..., params=..., inputs=..., labels=..., ...)`. Handlers receive ids (not ORM objects) and re-fetch, so nothing carries a stale snapshot across the queue boundary; a frozen input that must not change after the request (a deploy snapshot, an S3 session) travels in `inputs`.
- **What a task carries.** `key` is what its request competes for: the operation's name, then its subject's ids and its intent, e.g. `("apply_domain", domain_id)`. Requests that take turns on one subject share a key: a container's lifecycle requests (Deploy, Stop) are all `("lifecycle", container_id)`, so a container has at most one running and one pending. `name` is what the task does, shown and recorded; it is the key's first part unless given. `params` are JSON-safe ids and flags, passed to the task, recorded and shown. `inputs` are passed to the task only: never recorded, pushed or shown. `labels` say what people call the task's subjects, e.g. `{"Application": "shop / web", "Worker": "w1"}`, taken when it is queued, so a task built on opaque ids can be told apart on the Tasks page and history keeps the names the task was queued under.
- **Scopes** are `:`-delimited and hierarchical. The Manager's roots are declared in `services/manager/queue.py`: `worker`, `domain`, `certificate`, `backup`, `discovery`, `settings`, `release`, `metrics`. A parent scope conflicts with all of its children; siblings never conflict. The platform restore holds no scope: it is the queue's own entry (see Pause And Stop). Conflicting tasks are mutually excluded. Scope roots and executor lanes are registered independently; a task can require several scopes and use one existing lane. A task's scopes always include its subject's own scope, so two tasks with one key exclude each other.
- **Dispatch.** A one-second APScheduler interval tick calls `dispatch_tick`, which starts every pending task whose scopes conflict with no running task and no task queued before it. One dispatcher makes the scan race-free; a `threading.Lock` guards the queue because producers run on FastAPI/loop threads. **Among conflicting tasks the one queued first starts first**, both ways: a queued host-wide `worker:<host>` task that waits for one of that host's container tasks holds back the host's container tasks queued after it, and a container task that waits holds back a host-wide task queued after it. Tasks that do not conflict start together, whatever their place in the queue. A task waits only for running work and work queued before it, never for work queued later; while a broad task waits, the later work under it waits too, for as long as what runs before it. A new task is queued at the end, and nothing ever preempts running work. The scan (`next_to_start`) and admission (`admit`) are pure functions of plain data in `utils/queue.py`, which the queue applies under its lock.
- **Admission policy — `on_conflict` on `add_task`** (enum `OnConflict`; decided atomically under the queue lock). Admission decides whether a request queues a task, comparing **keys, never scopes**; mutual exclusion is **separate and always scope-based** — the dispatcher serializes conflicting scopes regardless of mode, so a queued task simply waits for its scope to free. `add_task` returns `Added(outcome, task_id)`: `accepted` (queued, its id), `existing` (the pending or running task with the key does it, that task's id), `busy` (REVISE: the task with the key is running, that task's id; nothing is queued or recorded) or `refused` (the queue is stopping: recorded as cancelled, its id; or a restore while the queue isn't running: no id).
  - `DEDUP` (default) — a pending task with the key, else a running one, is the request's task: `existing`. Prevents reconciler pile-ups and double-triggers (e.g. a second deploy of the same container). A task with *another* key holding a conflicting scope does **not** stop a new one — it is queued and waits behind that task. (So a background sync never rejects a user action; the action just defers behind it.)
  - `QUEUE` — always enqueue and wait, whatever the key. For work where every request must run.
  - `REPLACE` — cancel the **pending** tasks with the key, even when their scopes differ (a Domain moved to another worker), then enqueue and wait; a running one keeps running, with the new one as its successor.
  - `REVISE` — `busy` while a task with the key runs; otherwise cancel the pending tasks with the key, whatever their name, and enqueue. A container's lifecycle requests use it: the latest Deploy or Stop replaces the one waiting (a Deploy's replacement brings the latest snapshot), and none is taken while one runs. Automatic maintenance on the container has its own key, so a lifecycle request made during it waits behind it.
  - A weaker background request and a stronger explicit one (a 30 s ensure and a forced resync) get different keys, so one never stands in for the other.
- **`quiet`** (separate flag) — record only on failure, and keep the task's log lines only on failure (see below). A visible request matching a quiet task makes it visible: under DEDUP the pending or running task is recorded when it ends, and a running one's held lines are written at once, in order, then every later line; under REPLACE and REVISE the successor of a visible pending task is visible.
- **Persistence.** The queue calls a `record` hook (`Manager._persist_task`, which stores through `record_task` in `services/history.py`) at each terminal state. Only `completed`/`failed`/`cancelled` are written to the `Task` table, with the task's name, scopes, params and labels; running and queued tasks live in memory and are exposed via `snapshot()` for the UI (key, labels and params included, inputs never). A task keeps when it was queued (`QueuedTask.queued_at`) and when it started (`started_at`, set as the queue starts it); its row keeps them as `created_at` and `started_at` (none for a cancelled task, which never started), beside `finished_at`. `quiet=True` tasks skip the `completed` record, and the `cancelled` record when superseded or drained while queued, so scheduled housekeeping doesn't flood the table — failures still record. The table is the history of operations worth debugging; the same module reads it (`task_history`, `task_history_filters`, which `routes/tasks.py` answers with): a name, executor and status match exactly, a scope matches itself and its children, and a label matches any label holding the text, in any case. A quiet task also never makes a settings section show as applying. Status vocabulary: `cancelled` means the task **never started** (REPLACE or REVISE supersession, a cancel, the drain on stop); a task interrupted **mid-run** — the loop's shutdown cancels its coroutine — records as `failed` (started but did not complete). A forced stop (second signal, or the runtime's kill after its grace period) ends the process with the running task unrecorded.

- **Cancel.** `cancel(key, name, scope)` (`Manager().cancel_tasks`) removes the pending tasks that have the key, the name, and a scope at or below `scope` (each when given; a key or a scope is required) and records them as cancelled; a running one is left to finish, and the restore is never cancelled, since only it ends the restoring state. It returns the ids it cancelled. The Manager uses it for a container's queued Deploy, Stop, manual backup, volume restore or Delete, and for all of a container's queued tasks when it is deleted.

To make a deliberate user action coalesce with a recurring one, give both the same key and policy (DEDUP for "one at a time is enough", REPLACE for "the latest request wins"); the user's visible request keeps the task visible, and no per-operation locks or pending flags are needed.

## Pause And Stop

Sage never restarts or upgrades itself; the runtime stops and starts its container
(Settings → Upgrade lists the manual steps). Three states quiesce it. All are a state on
the `TaskQueue` (`QueueState`: `running`, `paused`, `restoring`, `stopping`), exposed as
`Manager().pause_tasks()` / `resume_tasks()` / `stop_tasks()` / `task_state()`:

- **Pause** (`POST /settings/pause`, `/resume`; Settings → Upgrade, banner while paused).
  `dispatch_tick` starts nothing: running tasks finish, queued tasks wait. Cron triggers
  keep firing and their tasks queue as usual, so a pause behaves like one long task holding
  every scope; DEDUP/REPLACE keep each recurring task to one pending copy, and they run on
  resume. In memory only, so a restart comes back running.
- **Restoring** (a platform restore). The restore is the queue's own entry
  (`TaskQueue.request_restore`, through `Manager().add_restore`), with no key, scope or admission
  policy: refused unless the queue is running, it sets the state at once. From then on every other
  request is refused, with no record; the tasks queued before it start as usual, and the restore
  starts once nothing runs or waits, so it runs alone. The UI shows a blocking overlay
  (`RestoreOverlay`) from `GET /api` — the state and `restoring_from`, the backup's timestamp — so a
  refreshed page shows the same. The restore ends the state with `Manager().end_restore()` in a
  `finally`, whatever its outcome; nothing waits behind it. When it took effect, what runs at
  startup is requested (`request_startup`) and the UI reloads. A stop signal cancels a restore that
  hasn't started; one that is running finishes first.
- **Stop signal** (`app/main.py`). The first SIGTERM/SIGINT shuts the scheduler down and
  stops the queue: queued tasks, and any added later (e.g. a follow-up queued by a
  finishing task), are recorded as `cancelled`; running ones finish, then the API server
  stops. There is no cap in Sage: the runtime's stop grace period bounds it. A second
  signal stops right away. The ingest server (`api_ingest`) doesn't handle signals, so
  worker logs keep arriving until the process exits.

`GET /api` reports that `state` and the `settings` status; the workers are the `workers` topic's, not part of it. It reads memory only (the certificate expiry too: `Certificate.load` reads the PEM at startup and after each write), because the pause banner and the restore overlay depend on it while the database is being replaced; if building the settings status fails, that part is `null` and the rest is still returned. While it isn't `running`,
`api.py` refuses changes with 503: every method but GET/HEAD/OPTIONS, except pause/resume.
A read is a GET. Pause comes only before a shutdown or an upgrade, so the settings test (a POST, since it takes the
values to check in its body) is refused too.

## Execution Modes And Pools

Pools (lanes) are defined in `app/utils/executor.py`. `LANES` holds the queue's executor pools, selected by a task's `executor` independently of its scopes; the rest are off-queue.

| Pool | Workers | Used by |
| --- | --- | --- |
| `LANES["platform"]` | 1 | `platform` lane — platform backup and restore, certificate renewal |
| `LANES["common"]` | 1 | `common` lane — settings and release checks, daily summary, backup catalog, log and history cleanup |
| `LANES["app"]` | 6 | `app` lane — containers' operations, recovery and orphan cleanup; workers' observation, setup, repair, add, removal and reboot; Domain applies and the pass; discovery; certificate copies |
| `LANES["metrics"]` | 2 | `metrics` lane — the minutely flush, metrics-store cleanup |
| `UPDATES_EXECUTOR` | 2 | the pushed topics of the UI's Socket.IO connection (off-queue) |
| `NOTIFICATIONS_EXECUTOR` | 1 | `Notifications.notify` webhook sends (off-queue, fire-and-forget) |
| `LOGS_EXECUTOR` | 1 | Workers' Vector log ingestion (off-queue, request-path; single SQLite writer) |

Worker counts are **fixed I/O-concurrency limits, not derived from `os.cpu_count()`** — these lanes run blocking network I/O (Tailscale, S3, httpx), so the manager's core count doesn't gate them, and in a CPU-limited container `os.cpu_count()` reports host cores rather than the container's allowance. `platform` runs one task at a time (the platform backup and restore, certificate renewal), so it stays at 1; `common` is the low-priority lane (settings checks, log cleanup, release refresh) and stays at 1 too, so its tasks run one at a time even where their scopes (`settings:<section>`) would allow more; `app`/`metrics` are sized for the realistic ceiling (≤10 workers, ≤50 apps, a couple of parallel deploys). Concurrent DB writes from `app`-lane threads are safe: Peewee keeps per-thread SQLite connections and the DB runs WAL + `busy_timeout`.

A task callable is sync or async, and `run_task` handles both:

- **async task** — awaited on the event loop; it fans blocking leaves out to its lane with `run_in_executor_with_context`. The task coroutine itself never occupies a pool worker.
- **sync task** — run entirely on its lane pool via `run_in_executor_with_context`.

**Ambient executor.** Before running a task, `run_task` binds its lane to the `active_executor` `ContextVar`. Any `run_in_executor_with_context(fn)` inside the task with no explicit `executor=` offloads to that lane, so the lane is declared once at `add_task` and inner call sites don't repeat it. `active_executor` is a `ContextVar`, so concurrent tasks on different lanes never see each other's binding.

**Fan-out is safe because orchestrators are async.** The Domain pass `await`s `asyncio.gather(...)` over its offloaded listings and removals. The async parent runs on the loop (holds no worker), so children just queue and drain on the lane pool — no parent-starves-child deadlock. A task is marked "running" once its scope is acquired, independent of pool-thread availability, so its offloads may briefly queue inside the pool; that is bounded throughput, not a stall. The invariant: **never make a sync task that submits to its own lane and blocks on the result** — that deadlocks a single-worker lane. (`run_in_executor_with_context` enforces part of this by requiring a running loop; keep any raw `submit` calls fire-and-forget.)

## Choosing A Task Shape

Three shapes are in use; pick by how the units of work relate:

- **One async task that `asyncio.gather`s its offloaded leaves** — for several *independent* remote operations that belong to one logical action and share one scope. The async parent holds no pool worker; the blocking leaves fan out and drain on the task's lane (bounded by its pool size). Use when the work is one operation internally parallelisable across targets: the Domain pass's listings of every worker, the zone and the tunnels.
- **Per-entity fan-out — one `add_task` per entity** — for *independent* work where each unit deserves its own scope, lifecycle, failure isolation, and DEDUP. The scheduler/orchestrator enqueues one task per entity with a per-entity scope: each container's lifecycle request, automatic backup or recovery (`worker:<host>:container:<id>`), each worker's setup, discovery files and certificate copy, each Domain's apply. One slow or failing entity only affects its own task, and each is independently coalesced/rejected.
- **Sequential `await`s in one task** — for *ordered or dependent* steps that must not overlap: stop a container *then* back its volumes up; write a worker's config files *then* `compose up`. Parallelising these would corrupt ordering, so they stay serial even though they hold their scope for the full duration.

The invariant above still governs all three: a fan-out parent must be async (never a sync task blocking on its own lane).

**When adding or changing a task:** pick the shape above, declare its `key`, `scopes`, `executor` lane, `on_conflict` and `labels` at the single `add_task` call site, and **add a row to the [Task Catalog](operations.md#task-catalog)**. Keep comments and docs describing the *current* behaviour only — never the prior design.

## Data And Persistence

Main database:

- `/app/data/data.db`
- SQLite
- WAL mode enabled
- some fields are encrypted through custom Peewee field types
- its schema follows the models: at boot and after a platform restore, `Database.reconcile_schema` creates the
  missing tables and indexes, adds the missing columns and drops those no model has, in one transaction with foreign
  keys off (SQLite's procedure for changes that rebuild a table, as adding a `NOT NULL` column does). It then
  compares each table with the model's, built in a throwaway in-memory database: a column whose type, `NOT NULL`,
  primary key or foreign key differs, or a table whose `CHECK` clauses differ (a new choice value is one), is
  refused, since SQLite can't change those in place. A refused column, or data that refuses the change (a unique
  index, the foreign key check), rolls all of it back and raises `SchemaRefused`: the boot stops with one
  `[CRITICAL]` line naming what failed and linking the running version's release, whose notes give the fix

A Domain row holds its configuration only. Its apply results and what was seen of its artifacts are memory, held by
the `Domains` owner, and its status is decided from them when it is read: after a restart it is pending until an
apply or a scan. Running and queued operations belong to the in-memory task queue.

Additional storage:

- metrics SQLite shards
- the logs, a SQLite file for each kind and for each application under `logs/` (`services/logs/`):
  - each application's shard, `logs/<project>-<app>.db`: its lines as Vector sends them, with a full-text index (FTS5). `GET /projects/{project}/applications/{application}/logs`, with a worker, a search, a window and a `limit` (at most `LIST_MAX_ROWS`, 1000, which is also the default), returns the newest `limit` that match, in the order they were stored, and whether there were more.
  - markers: what Sage did to a container, written when the action ends with its outcome and its task's id (`mark` in `services/manager/lifecycle.py`, `Logs.write_marker`): a line of the container's worker at the manager's time, in a stream of its own, `sage`, written on `LOGS_EXECUTOR`. A deploy, stop or delete, a failed action, a reset, a recovery, a backup, a volume restore and a worker's reboot each leave one; a marker that cannot be written is a warning, never the action's failure.
  - the search finds the text anywhere in a line, in any case: a `LIKE` on the index, which the `trigram` tokenizer serves for three characters or more, walked newest first and stopped at the cap. A shard made before the tokenizer is scanned.
  - the requests, `logs/requests.db`: Traefik's access lines parsed into start time, worker, host, method, path, status, duration, router, service, client and size, with no full-text index. `GET /requests/`, with a worker, a method, a status class, text in the host or the path, a minimum duration, only those a router matched (`routed`), a window and a `limit` (as the application logs'), returns the newest `limit` that match, newest first, and whether there were more.
  - the task logs, `logs/tasks.db`, with no full-text index: the lines the manager logs inside its queued tasks, read by task. `GET /tasks/{task_id}/logs` returns every line, in the order they were written. Each container operation is a task of its own, so a container's lines are its operation's task's.
  - every time in them, and every time a query or the retention compares with them, is UTC with nine digits of fraction and `Z` (`stored_time`), so that times compare as text: Vector and Traefik leave out trailing zeros.
  - A shard is bounded by size as well as age (`Logs.trim_shard`, `Logs.trim_total`; limits in `config.py`, see architecture.md). A shard file does not shrink when rows are deleted, but later writes reuse the freed pages, so the limit applies to the data (pages less free pages) and a file that is far bigger than the limit is rewritten (`VACUUM`). Deleting by `ts` uses an index on it, which also serves the time-range queries of the log view. Writes use `BEGIN IMMEDIATE`: the full-text index trigger reads before it writes, and a plain `BEGIN` fails with "database is locked" when another connection writes at the same time (the trim or the cleanup).
- on each worker, Traefik config and revision stamps under `/opt/sage`

## Scheduler Notes

The [scheduler and data-write reference](operations.md) shows the execution flows, exact
[scheduler timetable](operations.md#scheduler-timetable), scopes, synchronous route writes,
startup work and other model writers. `app/scheduler.py` owns the cron/interval registrations;
the operation queue owns execution and mutual exclusion.

## Task Catalog

The complete [Task Catalog](operations.md#task-catalog) lists every operation, its initiating
sources, scope set, executor, admission policy and state changes. It includes internal
follow-ups and distinguishes inline work from independently queued tasks.

## Settings Status

The `settings` part of `GET /api` gives each section (tailscale, cloudflare, s3, notifications) a state: `not_configured`, `applying` (a check or apply task is queued or running, except a quiet one such as the scheduled 5-minute check; its `task_id` is included; which tasks count, and with which params, is `APPLYING_TASKS` in `services/manager/settings.py`, written with `task(name, **params)`, e.g. `task("converge_worker", force=True)`, a worker's resync), `ok`, `retrying` (the last check failed in a way that clears on its own — network, timeout, 5xx/429; the 5-minute check retries) or `error` (needs the user — bad credentials, missing bucket or zone). Cloudflare is also `error` for invalid certificates or retained maintenance errors, including failed renewal of a still-valid certificate, and its entry carries `details` for the read-only page: account and tunnel ids (dashboard links), the platform tunnel's health from the 5-minute check (`healthy`, `degraded`, `down`, `inactive`; the check reads every tunnel of the account in one list call and Cloudflare keeps each one's health, `tunnel_health`; each worker's state shows its own tunnel's, `WorkerState.tunnel`), a certificate inventory (`id`, desired `names`, `valid`, `expiry`, `error`) for the manager, discovery and each worker, including pending retirement. Tailscale and Cloudflare are env vars, so they're always configured and checked as set. Notifications are independent channels (`CHANNELS` in `services/notification.py`, one settings field each): the section is configured once any channel is set, and its check covers every configured channel, reading each webhook without posting. One failing channel fails the section as `error` (`retrying` only when every channel failed in a way that clears by itself), the message names the failing channels, and the status entry's `channels` maps each failing field to its reason so the page can show it beside the field. `POST /settings/{s3|notifications}/test` checks the values in the request (saved or not) with side effects: the S3 test writes and deletes a marker object, the notifications test sends a message. Check failures never include a webhook URL (it holds the token).

A Save of a section (`settings_saved`, `services/manager/settings.py`) comes after its values are validated and saved: it installs the section's consumer (`S3`, `Notifications`) from the saved snapshot, asks for the backup discovery of a saved store, and queues a visible check of the section when it is configured, so the section shows applying until the check ends. The scheduled check (`request_settings_checks`, every 5 minutes and at startup) queues a quiet check of each configured section; both hold `settings:<section>` on the `common` lane with REPLACE on `("check_<section>",)`, and a quiet request while a visible check waits leaves it visible. A check records its result for the revision it checked (`Settings.record_check`), so a Save meanwhile drops it and the Save's own check counts.

Backups live in S3, which is optional: creating, discovering, restoring or downloading a backup is refused with 409 until S3 is configured, and the backup triggers skip. Deleting a backup removes its row and queues its archive's deletion, which is refused when its store is no longer the configured one.

**Downloading a backup** (`GET /backups/{b}/download`, also nested under a volume): the manager checks the object is still in S3 and answers with a presigned GET link that expires after 60 seconds (`BROWSER_LINK_SECONDS` in `services/s3.py`) and asks for the file as an attachment with a name that says which volume it is from. The browser then fetches the file from S3 directly, so no backup passes through the manager. The link is made when asked for, never stored or listed, and S3 checks its expiry when a request starts, so a download already under way isn't cut off. Whoever has a link can read the file until it expires, but the file is encrypted and authenticated with `ENCRYPTION_KEY` (see Settings → S3 for the `openssl` command that opens it). Who may ask for a link is the same as for every other route: whoever the tailnet policy lets reach the manager. Every presigned link states its validity: the links handed to workers for volume backup and restore use `WORKER_LINK_SECONDS` (1200 s; the upload starts after the archive is built, which the script's 900 s command timeout bounds) and reach `curl` through a `--config` file, so they stay out of its command line. The download route is a GET, so it also works while Sage is paused or restoring. The S3 endpoint has to be reachable from the browser, and over https (a browser may refuse to save a file from a plain-http endpoint opened from an https page).

A container's operations (Deploy, Stop, Delete, backups, a volume restore, recovery and reset) and a Domain's apply re-fetch their target by id with `get_or_none` and treat a missing row as a **quiet no-op**; a volume restore whose backup or target volume is gone is refused with a `rejected` event. Instant deletes can interleave with queued work; Domain artifacts keep their record id for the pass's cleanup. `remove_worker` returns for a missing worker, but fails loudly when its precondition re-check finds containers or internal domains. See [consistency boundaries](operations.md#consistency-boundaries).

Container create is an **instant route-side model write**, like most entities' CRUD — not a queued task: its command (`attach_container`) recomputes the application's `container_count` from the rows in its own transaction. Only a container's Delete is queued (it does remote cleanup).

**Container lifecycle.** Deploy and Stop are per-container tasks (`services/manager/containers.py`) on the container's lifecycle slot: key `("lifecycle", <container id>)` with REVISE, scope `worker:<host>:container:<id>`, so the latest request replaces a waiting one and a request while one runs is refused. A Delete outranks them: its request cancels every queued task of the container (`cancel_tasks(scope=…)`, the tasks holding its scope or one below it), then queues the Delete under its own key `("delete", <id>)` with REPLACE, which waits behind a task already running there; Deploy and Stop are refused while it is queued or running. Cancel drops a queued Deploy, Stop, manual backup, volume restore or Delete, each by its own operation. Config edits stay instant and apply to the **next** deploy: a Deploy request captures the application's `DeployConfig` once (Deploy all once for all its containers, each still its own task) and hands it to the task as an input. Under its scope the task re-fetches the row (gone: nothing to do) and reads the container's record from its worker through the `Containers` owner (`services/containers/targets.py`). It is refused, with a `rejected` event and no other effect, when the worker can't be asked, the record can't be read or is another Sage's, or its operation never settled (`lifecycle_refusal`); a record of this Sage from before a restore (another state generation) whose operation settled does not refuse it, and gives the new record nothing. The task then writes the record with the operation executing and its bound (the longest command it runs plus the worker's kill grace), stops the container (`docker compose -p sage-<id> down`) and checks nothing of it runs. A Deploy then writes the placement's files (`services/containers/rendering.py`), keeps the snapshot's volume directories and prunes Sage's others, runs `compose up --wait --build`, checks the container runs this deployment, and commits `applied`, `settled` and the operation with the worker's time of that check (`verified_on_worker_at`, rounded up to the second). A failure after the record was written stops the container and settles it stopped with the failure, or leaves the operation recovering when the stop can't be verified; there is no rollback. A Delete never waits for old work: an unsettled or unreadable record does not refuse it, and its record keeps the longer allowance of the work it takes over from (the longest, 905 s, for an unreadable record). It stops the container (`down --volumes --rmi local --remove-orphans`: the images the placement built go, a pulled image another container may be pulling or using stays), removes its placement directory, volume data included, records its success, then removes the row (`detach_container`, which unlinks its domains) and the `Containers` owner forgets it; what old work recreates afterwards belongs to no container and is an orphan. A forced Delete on a worker that can't be asked removes only the row, and its history says the worker was not cleaned up.

**Config-changed indicator.** A deploy's record keeps `applied.config_hash`, its snapshot's `content_hash` (SHA-256 over type, image, repo, path, env, args, build secrets, command, project env and the sorted (volume id, mount path) pairs). The container's view compares it with the current inputs' hash (`decide_container`): current, dirty, not deployed, or unknown without a readable record of this Sage.

**Container status.** A container's status is not stored: `Manager().container_states(containers)` decides it when asked (`services/containers/state.py`), from the record the `Containers` owner last read or wrote, the worker's newest report through `runtime_of`, the operation running on it (from the queue) and the fingerprint of its application's current inputs. The requests queued on it leave the decision as they are: they are in its state and payload apart (`queued`, in their turn, each with its task and whether Cancel takes it, which is when it is the user's request `request_cancel` matches). The decision (`decide_container`) gives the headline, the configuration comparison and, for an error, a reason; a record or runtime of this Sage from before a platform restore is `restored`, another Sage's is `authority`. Pages are told when it may have changed: the `Containers` owner when a read or write changes the record it holds, the report intake for the containers a report changed (`containers_reported`), a queue fact when an operation on a container starts or ends or a request on it is queued or cancelled, and the quiet minutely `observe_containers`, whose fact holds every container's last state and tells only the containers whose state changed with time alone (a report grown stale).

**Container reconciliation.** After telling the pages, `observe_containers` asks one pure decision per container what to do (`correction` in `services/containers/decisions.py`). Nothing is done about a container with an operation running on it. A record never read is read (`read_record`, quiet, under the container's scope, so it never crosses a write); everything else waits until the worker's newest report is current: a record whose read failed is read again, an unreadable record or an operation that never settled is recovered, this Sage's record from before a restore is reset, and so is a current runtime labelled as this Sage's that contradicts its record (running after a settled stop, another deployment, another state generation, or running with its record confirmed absent). Only a current runtime counts, and only one labelled as this Sage's; a reset only stops, and another Sage's record or runtime is left alone. Recovery (`recover_container`, a visible task under the container's scope) follows each incident, an operation or an unreadable record, with the time it was first seen on the Manager's monotonic clock (`Manager.recoveries`, gone once the record no longer needs it or the container is gone; a restart waits the whole allowance again): it contains at once, stopping the container and keeping the record recovering with the old work's allowance (an unreadable record becomes a reset operation with the longest, 905 s), and once that allowance has passed it stops the container again and settles the record stopped, failed by the interruption, under the current authority and with the applied deployment kept only from the same authority; a later Deploy then runs. A reset (`reset_container`, visible) reads the record and inspects the runtime again under the scope, decides again with that direct look, and only if a reset is still called for stops the container and records the cause, in words (`RESETS`), as its settled failure. Both append container events (interrupted, recovered or recovery_failed; reset admitted, succeeded or failed) and a log marker. Every 10 minutes `list_records` lists every placement directory and record on each reporting worker and asks for a read of each container whose listed record is not the one held; the listing, taken outside the containers' scopes, never replaces what the `Containers` owner holds.

**Volume backups.** A backup is per container (`services/manager/backups.py`): each archives the chosen volumes from its own placement (`containers/<id>/volumes/<volume id>`) with `backup.sh`, which encrypts the archive and uploads it through a presigned link, under `backups/applications/<volume id>/<container id>/<timestamp>.tar.gz.enc` in the store, and each archive gets its Backup row (`storage_id` the store's identity) once uploaded. The store's identity is captured when the backup is requested and held as `backup:store:<identity>:write:<container id>`; the task takes one session at its start and is refused when the configured store is another by then. A manual backup (`backup_stopped`, on the container's lifecycle slot) needs the container stopped: `request_backup` queues one per container of the application, refused while any runs, and its task checks again with the runtime read now; a failed archive fails it like any manual action (stopped, error), the archives that succeeded kept. An automatic backup (`backup_preserve`, its own key by container and volumes on the container's scope, so other volumes due while one waits or runs are backed up after it, queued each minute for the containers of an application whose volumes are due by their `backup_cron`, once per volume and minute) needs the container's last intentional outcome to have succeeded and its runtime to agree with it. Before it quiesces anything it records its baseline in the operation (whether the container ran, the deployment and Docker's ids of the containers it ran); it pauses the container with `docker compose stop`, archives, and resumes it with `docker compose start` whatever the archives did, checking that the same Docker containers run the same deployment. A failed archive with the runtime back fails the task and leaves the container's settled outcome as it was; a runtime that does not come back the same is stopped and settled failed. Refusals are `rejected` container events and failed tasks. A volume restore (`restore_stopped`, on the lifecycle slot) puts a backup of one of the application's volumes into any volume of a container of that application, on any worker, so data moves between containers and workers and one volume's data can go into another; tasks and markers name both volumes. It needs the container stopped, or never deployed, so a new container can receive data before its first Deploy; it holds its archive's scope, `backup:store:<identity>:archive:<hash of its key>` (the one a deletion of that archive holds), and is refused when the store changed or the backup or volume is gone. `restore.sh` downloads the archive through a presigned link, checks its MAC, unpacks it into its temp folder and moves the archived volume's folder into the target volume's place. Success commits the container stopped/succeeded; a failure leaves it stopped and in error, the volume possibly part replaced, and a Deploy allowed. No Backup row is written. Reconciliation recovers an interrupted automatic backup: it is paused as it is at once, and once the old work's allowance has passed brought back to its baseline, its settled outcome kept; one it can't bring back, or one from before a platform restore, is stopped and settled failed.

**Backup catalog.** The Backup table mirrors the configured store (`services/manager/backup_catalog.py`, with the pure `reconcile_catalog` and the writes in `services/backups/`). Each task carries the store's identity, holds `backup:store:<identity>` (discovery, retention) or a child of it (one archive's write, read or deletion), and does nothing to another store: a store configured since is never touched. Deleting a backup removes its row at once (`remove_backup`) and queues `delete_archive`, which deletes exactly that key from the store the row named, or is refused once that store is no longer the configured one. Discovery (`discover_backups`, at startup, on request, after an S3 save or a platform restore) lists the store's platform backups and volume archives, then reads the rows, and in one transaction drops rows of another store or of an object gone and adds listed backups, a volume archive only while its volume is configured (matched by id, its current name recorded); an archive whose deletion is queued or running is left out, so it does not come back. Retention (`prune_backups`, daily) deletes the store's backups older than `config.RETENTION_DAYS`, then their rows.

**Platform backup and restore.** A platform backup (`backup_platform`, `services/manager/platform.py`) holds the configuration only: `Database.snapshot` copies the live database with SQLite's backup API on the platform lane, empties the history (container and worker events, notifications, tasks) and the Backup rows (they mirror the store) and compacts it; it is encrypted, uploaded, and its Backup row records the key uploaded. It holds only `backup:store:<identity>:write:platform`, so it runs alongside other work. A restore (`restore_platform`) is a pseudo restart: it runs in the restoring state above, downloads the backup from the store it was requested for, and `Database.install` (on the platform lane) takes a safety copy of the live database, copies the backup in, reconciles its schema as at boot (an older backup is brought up to the models), gives it a fresh `state_generation` and empties the history and the Backup rows; any failure copies the safety copy back, and the safety copy is removed either way. Then the configured S3 settings are kept, the others' consumers installed, and the memory of the old configuration cleared as a fresh process starts (held records, worker setups and applied certificates, Domain facts, the Manager's bookkeeping); observations of hosts stay, being facts about the machines. Once the restoring state ends, `request_startup` asks for what runs when Sage starts: the workers' observation, whose setups follow by their stamps, and their paths; the containers' observation, which stops the containers whose records predate the restore; certificate renewal; the Domain pass and its discovery; backup discovery; the settings checks; the release check.

**Release, summary and history retention.** `Releases` (`services/releases.py`) holds the newest release as GitHub last answered and its version, the running one until a fetch finds another; `request_release_check` queues its refresh under `release`, quietly from the 4-hour schedule and at startup and visibly from the Upgrade page. The summary (`get_system_summary`, `services/manager/summary.py`) counts the workers, containers and Domains by their states from the read models the pages use, and adds the last 24 hours' deploys (an interrupted one failed), backups and error and warning notices; the home page reads it, and the daily message (`send_summary_notification`) sends it to the notification channels without a notice of its own, as an error when a notice was or a container or Domain is in error or a worker offline, and as a warning when a notice was or a worker is otherwise not online. The daily cleanup (`request_cleanup`) queues each part as a quiet task of its own: the metrics and logs past their retention, the history (`purge_history`: container and worker events, notifications, tasks) older than `RETENTION_DAYS`, and the store's backup retention.

**Notices.** `Notifications().notify(message, type, link)` (`services/notification.py`) logs a notice at its level, records it as a Notification row, pushes it on the `notifications` feed and sends it to the channels on their pool; `link` is the path of a page of the UI, recorded as the manager's full address so a channel's message links to it too. A notice names what the UI shows: a container as "<project> / <application> on <worker>" with its application's page (`container_notice`), a volume by its name, a Domain by its host, a worker by its hostname and a backup by when it was taken (`taken_at`), never by a key or an id. Notices are sent for a container's deploy, stop, delete, backup and restore and their failures; for what Sage did by itself (an interrupted operation settled, a container reset, an orphan removed, a worker's stack started, a certificate issued); for failed maintenance (certificate renewal and copies, Domain applies and the Domain pass, discovery, backup deletion and retention, a worker's setup, the platform backup and restore); for a worker added, removed (with what is left to remove by hand) or rebooted, and the failure of each; and for a worker going offline from an observed status and being reachable again. The daily summary goes to the channels without one.

**Orphans.** What this Sage left on a worker for a container that has no row there (a force-deleted container on a worker that came back, a row a platform restore does not have, a removal cut off) is an orphan: a placement directory or record in the listing, unless its record is another Sage's (worker setup refuses a worker another Sage claims, so a bare directory is this Sage's), or a runtime `sage-<id>` in the worker's newest report labelled as this Sage's. Placements are named by container id and an id is never reused, so a new container never meets an orphan's directory. `cleanup_orphan` (visible, under the orphan's own scope `worker:<host>:container:<id>`) re-checks that the row is still absent and that neither the record nor the runtime is another Sage's, then, while its record or runtime is left, runs `down --volumes --rmi local --remove-orphans` by project name and checks, and removes the placement, volume data included. When nothing of it is left, as when the worker's newest report was gathered before the container's Delete ended, it does nothing and sends no notice. An orphan whose record shows an operation that never settled, or whose record is unreadable, waits out that operation's allowance (905 s when unreadable) from when Sage first saw it (`Manager.orphans`); it has no routes, its domains having been unlinked with its row.

**Worker states.** Whether a worker's SSH server answers is held in memory by `Tailscale` (`observe_workers`), checked the way commands reach it (`Tailscale.reachable`: its banner through `tailscale nc`): reachable at the first answer, unreachable after two misses in a row or when the tailnet does not list it (one not seen yet stays unobserved until then), and unknown, with what was found before kept and no miss counted, while the manager's own node is cut off from the tailnet. Neither Tailscale's own `Online` flag nor `tailscale ping` is used: the flag is the coordination server's view and can lag by many minutes in both directions, and a disco ping gives a false offline while a path is cold. A worker whose SSH server stops answering loses its setup outcome, so it is set up again when it returns. Readiness needs a current report (`REPORT_STALE_SECONDS`) with its stack running and a positive Traefik check; a worker that has not reported is checking for `REPORT_STARTUP_GRACE_SECONDS` after the manager starts and not ready after it, and the grace never makes it ready. A container's runtime is unreachable while its worker's SSH server is (`services/containers/state.py`). An observed change between offline, not ready and online is recorded once as a worker event: connectivity when one side is offline, readiness otherwise.

**Adding, removing and rebooting a worker.** Workers are added by hand. The request (`request_add_worker`) takes the machine the tailnet lists (`Tailscale.find_peer` resolves a hostname or Tailscale IP; one it does not list is refused, and so is the manager's own name), named by its hostname in lowercase (`tailscale nc` resolves a name in any case) and its subdomain, one DNS label and unique, which its internal names are under (`<name>.<subdomain>.int.<domain>`); the command writes the row (`services/workers/commands.py`), and the add task, holding `worker:<host>`, looks at the worker (`Tailscale.observe_worker`) and runs its first convergence inline. A worker whose first setup fails, or whose SSH server does not answer, is kept: it shows not ready or offline and is set up again like any other, and the add's failure stays in its history. What an earlier worker of that hostname left needs nothing more: the convergence reuses its tunnel and rewrites its record, a stale infra stamp is applied again, its container placements are orphans the container cleanup removes, and another Sage's owner stamp is refused. A new subdomain (`set_worker_subdomain`, then `worker_subdomain_changed`) converges the worker, after a convergence already running (REPLACE, since that one read the old subdomain), which moves its record, applies its internal and TCP Domains again and renews its certificate. Removal (`request_remove_worker`) is refused while the worker has containers or internal domains (`removal_refusal`, checked again by the command under the task); the task, under `worker:<host>`, deletes the row, then, when the worker answers and its owner stamp is this instance's, runs `docker compose down --remove-orphans` and removes Sage's home on it, deletes its DNS records (by their comment, `sage-worker-<hostname>`) and its tunnel (`sage-<domain>-<hostname>`; one Cloudflare will not delete is left with a warning), and has the sources forget it. A worker that does not answer is removed from Sage only, and the VM has to be cleaned up by hand. A reboot (`request_reboot`, `worker:<host>`) is refused unless the worker answers and is set up; it reads `/proc/sys/kernel/random/boot_id`, sends the reboot once and every `WORKER_REBOOT_POLL_SECONDS` reads the boot id again: on a new one it converges the worker inline, and it is verified once the worker is ready on a report that arrived after the new boot was seen. Not verified within `WORKER_REBOOT_LIMIT_SECONDS`, it fails with how far it got (still the old boot, not ready, or unknown); a later return is ordinary observation. The task holds the worker while it waits, so a manager shutdown during a reboot waits for it. Each step is a worker event (add, remove, reboot: admitted, then succeeded or verified, failed or rejected).

**Worker status.** In this order: `removing`, `adding` or `rebooting` while that worker task is queued or running; `offline` when its SSH server is unreachable; `checking` while the manager is cut off from the tailnet, or the worker is not observed or not set up since the manager started; `not ready` when another Sage owns it, its setup failed, it is not reporting, part of its stack is down or Traefik does not answer (the reason says which); otherwise `online`, the only status where it is ready (unknown while checking). Its `path` is how the last `tailscale ping` reached it (`direct`, or `relay <region>` for a DERP relay), refreshed every 60 s for the reachable workers (`refresh_worker_paths`).

Worker convergence (`converge_worker`, `services/manager/workers.py`) holds the worker's own scope `worker:<host>`, so it waits for that worker's container tasks and holds them back while it rewrites and restarts the stack; other workers' work goes on. `observe_workers` asks for it for each reachable worker not set up since the manager started or since it came back, whose address differs from the one its last setup applied, or whose last setup failed or was refused at least `WORKER_SETUP_RETRY_SECONDS` ago (the Manager keeps when, `setup_failures`). It reads the worker's stamps, refuses another Sage's worker (setup outcome `refused`, shown as not ready, recorded once as an ownership event), claims an unclaimed one, makes the worker's tunnel and its `*.<subdomain>.int.<domain>` record, and, when the infra stamp differs from `infra_fingerprint` or on force, writes the stack's files (`render_infrastructure`), runs `compose up --wait`, restarts Traefik, Vector and Telegraf and writes the stamp last; otherwise it runs `compose up -d` to start what is stopped. It records the setup outcome and the address applied (`Workers`), and an applied or failed infrastructure as a worker event. Force (`request_convergence(host, force=True)`, its own visible key) applies whatever the stamp says and deletes no stamp. Stack repair (`repair_worker_stack`) starts what is stopped of a set-up worker that is not ready because part of its stack is down, Traefik does not answer, or it has been silent for `WORKER_STACK_SILENCE_SECONDS`, at most once per `WORKER_STACK_REPAIR_COOLDOWN_SECONDS` (`stack_repairs`), and only while the worker is still reachable and set up when it runs; it holds the stack's own scope `worker:<host>:stack`, so a deploy does not delay it while convergence and reboot exclude it. Docker does not retry a container whose start failed, which is what happens to Traefik when a worker boots before its Tailscale address exists. The manager issues required certificates in-process (`Certs`, via ACME DNS-01), sequentially within the global operation; Traefik watches its dynamic configuration and reloads certificate changes without a restart.

## Cloudflare Request Failures

`Cloudflare.request` classifies HTTP 429, HTTP 5xx and transport failures as retryable. It uses
`config.CLOUDFLARE_RETRY_DELAYS` (1 and 2 seconds: at most three attempts). A valid `Retry-After` on an error is parsed
as nonnegative delay seconds or an HTTP date in UTC. The resulting `CloudflareError.retry_after_seconds` is a
minimum wait in the shared `retry`/`retry_async` helpers: each wait is the greater of that minimum and its configured
backoff. Missing or malformed values use the configured delay; a past date contributes zero. The header does not
add attempts or make a permanent failure retryable. The final error retains Cloudflare's reason and its wait hint.
See [Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3).

Waiting occupies the operation's existing executor thread and keeps its task scopes held. There is no shared
cooldown, proactive pacing or separate retry scheduler. DNS/API budgets are described in
[architecture.md](architecture.md#dns-and-api-limits).

Domain apply and discovery reconciliation own their failure notifications. A failed apply holds its phase and error
for the fingerprint it applied and sends an error notification linked to Domains before failing the task. Parent failures aggregate inventory, cleanup and publication errors into one
notification per failed pass; other workers still progress and successful Domain proofs remain intact. The queue
records failed tasks, including quiet tasks, in task history. UI notifications use the existing notification feed.

## Domain Apply And Discovery

`apply_domain` owns one Domain's routing file and DNS records. `request_domain_applies` captures each Domain's
`DomainConfig` and queues its apply with `REPLACE` on `("apply_domain", <id>)`, holding `domain:<id>`, its full name
(`domain:host:<fqdn>`) and its route on the worker it goes to (`worker:<host>:routing:<id>`). It does not hold the
container's scope, so a long deploy does not hold back a Domain edit; the worker's convergence and reboot, holding
`worker:<host>`, exclude it. The task re-fetches the row: a missing one is a quiet no-op that drops what `Domains`
held of it, and a worker or name that changed since the request queues it again with the right scopes before any
remote work.

The route follows the container's `routable` when the apply runs (`route_goal`): present while it can be routed to,
absent once it cannot or the Domain is unlinked, and left as it is while that is not known, so missing observations
never remove a working route. A container is routable when its record and runtime make it active, whatever operation
runs on it: an operation records that it began before it stops the container. The file is written or removed only on
a worker whose SSH server answers and whose setup succeeded since the Manager started; otherwise it is left for a
later apply. `domain-<id>.yml` (`DomainConfig.route`) starts `# sage-domain <id> <fingerprint>`, the proof a scan
reads back, and has router/service identity `domain_<id>`, the current host and port, and the container's runtime,
`sage-<container id>` on `sage_default`, as backend. HTTP names also get a `HEAD /ping` probe answered by Traefik's
`ping@internal` with `Timing-Allow-Origin: *`, `Access-Control-Allow-Origin: *` and `Cache-Control: no-store`; GET
reaches the application. `sync_file` writes through a temporary name and renames it, and Traefik's file provider
reloads the directory. An unlinked internal or TCP Domain removes its file from its namespace worker; the route an
unlinked public Domain left on its old worker goes with the cleanup of routing files.

Routing is applied before DNS. Each Domain owns the exact comment `sage-domain-<id>`: a linked public name points
at its worker's tunnel (`<tunnel id>.cfargotunnel.com`), and a stopped container keeps it to avoid negative caching.
Internal, TCP and unlinked Domains have no record under their comment. Grouped DNS sync removes obsolete names and
corrects the requested fields, including proxy mode. Worker wildcard records belong to worker setup.

`Domains` holds, by Domain id, the outcome of the last apply for the fingerprint it applied (succeeded, or failed with
its phase, `route` or `dns`, and error), what was last seen of its routing file on the worker it was looked for on,
and whether its DNS conformed for a fingerprint; an apply records what it did to each. An apply succeeds only when it
applied the route too: one that left the route because its worker could not be written to or its container's state
was not known records no outcome. `decide_domain` turns these, the container's `routable`, the worker's readiness and
the queue into `syncing`, `pending` (with why), `synced` or `error`. The fingerprint covers the rendered inputs, the
container and worker the route goes to and the routing templates, not whether the container runs; renaming its
application does not change it. A result of a configuration edited during the apply counts only for the fingerprint
it applied, and the edit queues another apply. A failure fails the task and is not retried by itself: the scheduled
scan tries again. `Domains` publishes the Domain's pages when what it holds changes, and the queue's applies show and
end `syncing` the same way: `domains` and the linked application and worker topics. A worker's readiness change
shows its Domains again. Application rows hold no domain progress state.

The nullable `Domain.container` foreign key gives each Container its `domains` backref. A container's payload
(`container_payload`, `services/containers/state.py`) includes that collection, each through the Domain's payload
(addresses, sync state and binding). Application and worker details nest these container payloads;
applications have no domain collection of their own. Deleting a container leaves its Domain records unlinked.

Domain also owns nullable `cors_origins`, a JSON list validated by `OriginListField` on every database write.
Blank derives the record's own current HTTPS origin. Explicit origins are normalized HTTP(S) origins, `*` or `null`;
paths, queries, fragments, userinfo, control characters and subdomain wildcard patterns are refused. The effective
list enters `DomainConfig` and its fingerprint. Editing it invalidates routing proof without changing deployment
inputs. Unrelated saves preserve concurrent edits to the field. A rename never updates other records' explicit lists.

Each HTTP record file contains `domain_<id>_cors`, attached to the application router after the internal allow-list.
Its stock headers middleware clears a backend Allow-Origin value, then supplies the matching configured origin;
an unmatched request cannot inherit a backend's allow-all value. Other application headers are preserved.
Fixed credentialless preflight defaults allow methods `*`, headers `*` plus explicit `Authorization`, with a
300-second preflight cache and `Vary: Origin`. Methods, headers and cookies have no editor. The higher-priority
HEAD probe uses the separate shared probe middleware. TCP clears its inapplicable override and has no CORS middleware.

Domain edits, the end of a container's Deploy or Stop (or the failure of either), a container becoming routable or
no longer routable at the minutely observation (a change between unknown and known requests nothing), container
deletion (the Domains it unlinked), worker subdomain changes (its internal and TCP Domains) and a worker's
resync (the Domains routed to it) request the affected Domains' applies. Container creation has no domains to apply.

`reconcile_domains` is the pass over every Domain's artifacts: it sees what they are, removes those left behind and
requests the applies they call for. It is quiet, holds `domain`, so no apply runs while it looks and nothing it records
can cross an apply's write, and uses the app lane. Requests coalesce with `REPLACE` on `("reconcile_domains",)`: one
that arrives while a pass runs gets a pass of its own after it. It runs every five minutes and at startup, at the end
of each worker setup, so a returning worker's routes are seen and applied again at once, and after a change that
leaves a route behind: a container deletion that unlinked Domains, and Domain edits and deletes.

It lists in parallel the routing files on each worker whose SSH server answers and whose setup succeeded (the first
line of each, `routes_command`, with a 30-second timeout), every record of the zone and the account's tunnels (two
Cloudflare calls), and only then reads the Domain rows, so a Domain created meanwhile is never taken for one gone. On
each listed worker it removes, in one command, the routing files whose Domain is gone or goes to another worker (it
moved, or it is an unlinked public Domain, which goes to none); a file whose Domain goes to that worker stays whatever
its container's state, since whether it is there is its apply's. It deletes the records under each `sage-domain-<id>`
comment whose Domain is gone. Only exact `domain-<id>.yml` files and `sage-domain-<id>` comments carry Domain
ownership; infrastructure, certificate and discovery files and other records are left alone. No old-worker list,
tombstone or cleanup column is stored: a worker that was out keeps its old routes until it is set up again, which
removes them first.

It then records what it saw in `Domains`: each Domain's route on its worker (present with the fingerprint its header
names, absent, or unreachable when that worker was not listed) and, when the zone and the tunnels were both listed,
whether its records are exactly those its configuration gives (names, CNAME, target and proxy mode). A listing that
fails is unknown, never an empty inventory. It drops what `Domains` held of Domains gone and requests the apply of each
Domain in error, drifted, or not applied (after a restart an unlinked public Domain has no route to observe, so it is
applied once); one pending because its container's state is not known or its worker is not ready waits. A failed
listing, removal or DNS deletion fails the pass after the rest of it ran, without changing a Domain's own results.

A worker's setup, holding `worker:<host>`, lists the worker's routing files right after claiming it and removes those
of Domains gone or bound to another worker before its stack starts: by binding only, so a container whose state is not
known loses nothing.

Discovery lists every Domain with a service and a full name, its pool with it, whatever the state of its worker or
container, unlinked ones included: each name has a Boolean availability parameter, `;online` for true and
`;online=?0` for false (`discovery_services` in `services/domains/discovery.py`). A name is online only while its
Domain is synced for its current configuration, its container can be routed to and its worker is ready, as the
Domain's state decides them when a worker's files are written, and offline once one of those is known not to hold:
unlinked, its container not routable, its worker not ready, its Domain in error or drifted. Otherwise it is not known
yet: an apply of its Domain is queued or running, or what its Domain, container or worker is has not been observed
since the Manager started. The names follow the Domain table at every publication, whatever is known of them; only
the availability of a name not known yet waits: it keeps what the worker's file publishes for it (offline where it
publishes none) for `UNKNOWN_HOLD_SECONDS` (two minutes) at most, from when its Domain's apply was queued while one is
under way, else from the Manager's start, and is offline after that. So a restart rewrites nothing, and an edited
name keeps its availability until its apply ends. Discovery neither probes targets nor runs container reconciliation.
All-offline services still have a discovery router.

`publish_discovery(<hostname>)` writes one worker's files. `request_discovery` queues it for every worker reachable
and set up, each quiet with `REPLACE` on `("publish_discovery", <hostname>)` under `worker:<hostname>:discovery`, so
the worker's setup, reboot or removal, holding `worker:<hostname>`, excludes it while the other workers' go on; the
task checks again that the worker is there, reachable and set up. Each file's first line is its proof,
`# sage-discovery <fingerprint>`: the fingerprint of its service's names with their availability, the domain, the
templates and the response cache time. The task lists the first line and the `Sage-Endpoints` line of every
`_discovery-*.yml` on the worker (`files_command`, `parse_file_listing`): each file's proof and the availability it
publishes for each name. It writes each service's file whose proof is not the one its service gives now (every one
when forced), then removes in one command the files of services no Domain has.
A failure part-way leaves the files not written with their old proof, and the next publication writes them.

Every worker reachable and set up receives `_discovery-<namespace>-<service>.yml`, a router on `discovery.<domain>`
(public, `web`) or `discovery.int.<domain>` (internal and TCP, `websecure`, tailnet only), matching
`?service=<service>` and HEAD, GET or OPTIONS. Its headers middleware supplies `Sage-Endpoints` (names, pools and
availability as an RFC 9651 list), `Cache-Control: private, max-age=30` (`config.DISCOVERY_CACHE_SECONDS`) and CORS.
CORS preflight max-age is 300 seconds.

`publish_discovery_dns` (quiet, `REPLACE` on `("publish_discovery_dns",)`, under `discovery`), queued with every
request, makes the shared records from one list of the zone: `discovery.int.<domain>` an A record (DNS-only) to each
worker reachable and set up, ready or not, under `sage-discovery-<hostname>`; and `discovery.<domain>` a proxied CNAME
to the platform's tunnel under `sage-tunnel`, which every worker's platform connector serves. Whether a worker has its
record is `discovery_member` (`services/workers/decisions.py`): the record of a worker known to be out (its SSH server
unreachable, its setup failed or refused) or removed is deleted. That of one not known yet is left as it is: while the
Manager is cut off from the tailnet, and otherwise (not observed, or not set up since the Manager started) for
`UNKNOWN_HOLD_SECONDS` after the Manager started, after which it is out. So a restart deletes none.

Discovery is requested at the end of every Domain apply, failed ones included, and of every Domain pass, which every
five minutes also repairs it; when whether a container with a service's Domain can be routed to changes, its becoming
known included (the minutely container observation); and when the ready workers, or the workers reachable and set up
with their addresses, change (each worker observation and report). A worker's setup and a subdomain change reach it
through the pass and the applies they request. Published availability reflects detection and report lag, queue and
worker I/O, and the client's cache lifetime; rapid changes coalesce into the latest state.

**Revision stamps make stale worker state detectable.** Every worker carries
`{worker_home}/revisions/` — one hash stamp per artifact class, written by
whatever applied the artifact, and deliberately **outside** `traefik/dynamic/`
(which Traefik watches). Expected hashes are computed from current configuration on demand. The stamps:
`infra` — `infra_fingerprint` of the stack's inputs and templates, written
as the **final step** of `converge_worker` so it is a commit marker (an interrupted
setup leaves the old stamp and gets retried); `certs` — a JSON map of independent `worker` and
`discovery` proofs, each folding the leaf-chain fingerprint and certificate-template digest, written last by
`distribute_certificates` and read into `Workers` at each setup. Each worker's state carries both certificates
(`certificates`, by role) from `Certs`, judged against its current subdomain and the proofs it applied; each holds
`covered`, `synced`, `expiry` (only for a valid matching applied proof) and `error`, and stays out of readiness;
`owner` — `<instance id> <domain>` of the Sage that manages the worker, written first by `converge_worker`, which
another Sage refuses to set up. Each Domain's applied proof is its routing file's first line, `# sage-domain <id> <fingerprint>`,
and each discovery file's is its own first line, `# sage-discovery <fingerprint>`.

ACME orders require the advertised `config.ACME_PROFILE` (`tlsserver`), which issues 45-day certificates. An
unavailable profile fails issuance rather than falling back. Renewal uses the actual signed expiry and
`config.RENEWAL_THRESHOLD_DAYS` (seven days), read when checked. The daily/startup certificate task only issues
when the certificate is invalid or due; a failed renewal leaves its current PEM files in place.

`Certs` stores manager material under `certs/manager`, shared discovery under `certs/discovery`, and each worker
under `certs/workers/<hostname>`. Exact SAN equality and a matching private key are required. A rename/removal
moves the public leaf into that owner's `retired/` directory and deletes its private key. Public leaves remain
until revocation succeeds, `alreadyRevoked` is returned, or they expire; startup/daily/on-demand maintenance finds
removed owners on disk. A revocation failure blocks that owner's replacement and reports an error, while the pass
continues with independent owners. A certificate returned after its owner changes is retained for revocation.

Renewal (`renew_certificates`, under `certificate`) sets the worker names from the Worker rows, then takes one
certificate at a time: it retires what no longer covers its names, a removed worker's included, and issues what is
missing or due. A worker's setup requests it when the worker's own or the discovery certificate does not cover its
names, and so do a worker's removal and subdomain change. Renewal ends by requesting the distribution of every
worker reachable and set up; a worker's setup requests its own when the proofs its `certs` stamp holds are not the
current ones.

`distribute_certificates(<hostname>)` copies a worker's own and the shared discovery certificate to it: quiet with
`REPLACE` on `("distribute_certificates", <hostname>)` (forced, visible, on its own key with `"force"`), under
`certificate:worker:<hostname>`, so renewal, holding `certificate`, never has a chain replaced under a copy, and
`worker:<hostname>:tls`, so the worker's setup, reboot or removal excludes it while other workers' copies go on. It
checks again that the worker is there, reachable and set up. A worker uses
`traefik/certs/managed/{worker|discovery}-<hash>.{pem|key}` (keys `0600`) and a stock `tls.certificates` list in
`traefik/dynamic/certs.yml`; only valid material is published. Complete pairs precede the list; unwanted managed files
are removed and proofs written last. The actual file set and the list's digest are checked as well as the proofs, so
a missing or unwanted file is repaired under a matching stamp; forced, everything is written again. Certificate-template changes belong to these proofs, rather than the infrastructure stamp.
Valid material remains published through failed routine renewal. Offline edited workers converge when they return;
offline removed VMs still require manual decommissioning. Shared discovery remains an explicit shared key.

Artifact fingerprints fold in their **template files themselves**, since templates ship inside the image and a changed template must reach the workers without another input changing. A worker's infra stamp holds `infra_fingerprint` (`services/workers/rendering.py`): its address, the manager's, the domain, the platform's and the worker's tunnels, the version, the report interval and the stack's templates; `render_infrastructure` writes the files from the same inputs, the Compose file as it is (its `${...}` are Compose's own, read from the `.env` beside it). Domain routing has its own digest (`ROUTING_DIGEST` in `services/domains/config.py`), as have discovery (`TEMPLATES_DIGEST` in `services/domains/discovery.py`) and certificates (`TEMPLATE_DIGEST` in `services/certs.py`), each computed once at import. Templates pushed unconditionally, such as the revision writer and the per-application compose/backup/restore scripts, need no digest. A container's deployment is its record's `applied.deployment_id`, which its runtime carries as the `sage.deployment` label with the instance, generation and container ids; the status decision compares them, so a runtime of another deployment or authority is an error, not active.

**Name components are collision-free by construction.** Every name that ends
up in a filename or constructed identity is **rejected, never cleaned**, on any
write path: project and application names, volume names and setting keys are
`AlphaNumericField` (lowercase alphanumerics starting with a letter), and
domain names, services and pools and worker subdomains are `LabelField` (one
DNS label: lowercase letters, digits and dashes, at most 63, not starting or
ending with a dash). Project/application names are derived from the free-text
`label` via `values.names.derive_name`, which raises when no valid name
can be derived (no letters, or digits before the first letter); the others are
typed directly by the user and rejected as-is on violation. A qualified name therefore contains exactly one dash, so it is
injective and no qn can dash-prefix another — every `{qn}-` glob and prefix
match below is exact. A container's runtime is named by its id, `sage-<container id>`, a unique Docker DNS name on
`sage_default` that its Domains' routes send to. Worker
hostnames are Tailscale's identity, not ours: the one remaining
`CleanCharField` (dashes allowed, still rejected-not-cleaned on violation).
`ValueError` is the house convention for invalid user input: a global
exception handler in `api.py` maps it to a 400 carrying the message, so
routes and commands raise it bare.

## Settings And Worker Resync

Cloudflare and Tailscale are configured by required env vars (`DOMAIN`,
`CLOUDFLARE_API_TOKEN`), so changing them is a
restart. On start, `setup_domain` sets up the new domain (zone, tunnel, the manager's
record, the manager's certificate) before serving, the `renew_certificates` startup run
issues the shared discovery and per-worker certificates, and each worker's setup converges it once it
answers: the `infra` stamp includes the domain and the ids of the
platform's tunnel and of the worker's own (a new account means new tunnels), the `certs` stamp proves the two applied certificate pairs, and each
Domain configuration fingerprint includes its full host. Resources of a previous domain or account stay
in Cloudflare.

The `Setting` table holds S3 and notifications only; every field is optional and set from
the UI. A database, or a restored backup, can hold rows for keys that aren't defined; they
are dropped whenever the settings load (`Settings.load_all`), and `routes/settings.py` only
serves defined keys. A Save (`PUT /settings/{section}`) puts the fields sent over the saved values,
checks them locally (`validate_section`: only the section's fields, text or nothing in each, S3's all or none), saves
them (`Settings().set`) and hands on to `settings_saved`, which installs the consumer from the saved snapshot, asks a
saved store for its backup discovery and queues a visible check.

A worker's resync is its forced setup (`converge_worker` with `force`, its own visible task; "Resync workers" queues
it for every worker). It is the one manual repair, for what Sage keeps on the worker that looks right to its stamps
and headers but is not: it writes the stack's files and restarts the stack whatever the `infra` stamp says, then
requests a forced certificate distribution, the applies of the Domains routed to it (an apply always writes the
routing file) and a forced discovery publication on it, and a renewal when its own or the discovery certificate does
not cover its names. Shared records (each Domain's DNS, the discovery records) need no manual action: the Domain pass
and the discovery DNS publication correct them every five minutes, and the daily renewal retries a failed issue.

## Templates

`app/templates/` contains generated runtime inputs for:

- worker compose files
- worker Traefik config
- worker Vector config
- worker application backup and restore scripts
