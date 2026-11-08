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

The ingestion API is separate in `app/api_ingest.py` and serves what the workers push on port 9001: Telegraf's reports at `/api/telegraf/report` (kept in memory by `services/worker_reports.py`, read by the operation queue; a report from a name that is not in the worker list is refused with a 404 and logged once) and Vector's logs at `/api/vector/logs`: Traefik's and the applications' lines (the worker's Vector ships Traefik and the containers with the `sage.deployed_at` label, and sends each event with only the container's name, the time, the stream and the message, in batches of at most 4 MB). The intake refuses a push over `LOG_INTAKE_MAX_BYTES` with a 413, and decodes and writes it on `LOGS_EXECUTOR` (`Logs.ingest`), off the event loop, one write per file: Traefik's access lines go to the requests in columns and its own lines are dropped, each application's lines to its shard. A line that is not an event is skipped and counted in a warning. A write that fails answers 503, which Vector sends again after; a file already written then gets that batch twice.

## Pushed Updates

`services/updates.py` (`Updates`) pushes state to the UI's Socket.IO connections, and `routes/updates.py` holds the Socket.IO server (`sio`). A topic is a resource: a connection that follows it is sent its whole state each time it changes, as the JSON of the REST route for it. Subscribing sends nothing; a page loads the first copy from the route. A feed is a list that grows: a connection that follows it is sent each row that is published to it.

- **Transport.** Socket.IO at `/api/socket.io/` (`python-socketio` and `python-engineio`, pinned in `requirements.txt`). The browser starts with long-polling and upgrades to a WebSocket, which uvicorn serves through `websockets` (also pinned); reconnecting, and falling back to long-polling, are the libraries'. `api.build_asgi_app` gives the hub the `TOPICS` table (`Updates().serve`) and puts the Socket.IO server in front of the FastAPI app, so the API's refusal of changes while paused (an HTTP middleware) does not apply to it, and in production wraps both in `OnlyManagerAddress`.
- **Events.** Client to server: `subscribe` and `unsubscribe`, with a topic name. `subscribe` is acknowledged once the connection is subscribed, which is the point from which every change reaches it. Server to client: `state` `{topic, data}` (for a feed, `data` is a list of rows, one list per published batch) and `topic_error` `{topic, status}` for an unknown topic (404) and for a followed topic whose route answers with an error instead of its state (a 404 for a worker or an application that is gone; the hub sends it in place of a state and does not log it). A message that is not a topic name is ignored.
- **Topics** are the `TOPICS` table in `routes/updates.py`: a loader (the REST function, so a route and its topic cannot differ) and the sources whose change makes it worth computing again. The names are the `UpdateTopic` enum (state topics) and the `UpdateFeed` enum (feeds), and the sources are `UpdateSource` (all in `services/updates.py`, each value unique), so a name cannot be reused by accident and `publish` takes only a feed. `info` (`GET /api`) follows `queue_state`, `restoring`, `settings_applying`, `checks` and `latest_version`; `workers` (`GET /workers`, each worker's status, path and container count) follows `worker_list`; `tasks` (`GET /tasks`, the running and queued tasks) follows `tasks`; `backup-status` (`GET /backups/platform_backup_status`) follows `backup_status`; `backups` (`GET /backups`, the platform backups) follows `backups`; `release` (`GET /info/release`) follows `release`. Two topics take an argument and are followed as `name:argument` (`Topic(parameterised=True)`, whose loader is given the argument; a name with an argument that the topic does not take, or without one that it needs, is a 404 like an unknown topic, and a name is forgotten when its last follower leaves): `worker:<hostname>` (`GET /workers/{hostname}`: the worker's row, its containers with their applications, and `infra`, `online`, `status` and `path` from its entry in the worker list) follows `worker_entry` and `worker_containers`; `application:<project>/<name>` (the application route's payload) follows `application`. Their sources are keyed: `Updates().changed(source, key)` sends only the topic whose argument is that key, a keyed source never reaches a topic without an argument and an unkeyed one never reaches a topic with one. `notifications` and `task-history` are feeds and have no loader: the routes `GET /notifications` and `GET /tasks/history` (the newest `limit` finished tasks, at most `LIST_MAX_ROWS`, newest finished first, filtered by name, scope and its children (`*` for any characters, in any case), executor, status and a window the task was active in; with how many match, and the filters' choices at `GET /tasks/history/filters`) are where a page gets the rows it starts with and the ones it missed. Three more feeds carry the logs as they are written: `task-logs:<task>` and `task-logs:<task>/<part>` (a container's part), published by the drain thread one line at a time; `application-logs:<project>-<app>` (the shard's name) and `requests`, published by the intake, one batch per file and push. The first two are parameterised feeds (`Feed(parameterised=True)`, followed as `name:argument` and forgotten when the last follower leaves), and `publish(feed, rows, key)` sends a parameterised feed's rows to the followers of that key. A feed nobody follows is not even encoded: a view that follows it from then on loads what was stored before. Their routes take `after_id`, the last row a view holds, which is what a view loads each time the socket connects.
- **Sources** only say "I changed", with no detail, and their owner fires one only when what it owns differs from before, so the hub does not compare anything. `Updates().changed(source)` is safe from any thread (it hands the change to the event loop) and does nothing before the first connection. Several come from the queue, and each is declared by the owner of the data. A `Fact` (`services/updates.py`) is a value an owner works out from something that changes: `check()` works it out and, when it differs from the last time, calls its callback with the new value and then fires its source; the value is worked out and handed to the callback under a lock of its own, since the queue's hook runs on many threads, so the callback gets the values in the order they were worked out. `Manager.queue_facts` is the list of every owner's facts, put together in `Manager.__init__` from `tasks_queue_facts` (the queue: `tasks`, the snapshot of the running and queued tasks, and `queue_state`), `settings_queue_facts` (`settings_applying`, the task that makes each section apply), `platform_queue_facts` (`restoring`, the backup being restored, and `backup_status`, whether a platform backup runs and whether backups are being discovered) `domains_queue_facts` (the records being synced, which fires `domains` and the affected application keys) and `workers_queue_facts` (the workers being added, removed or rebooted, which is given to the worker list and is no source). `Manager._queue_changed`, the `TaskQueue` hook (called outside its lock on any change of the running or queued tasks or the state), only checks each one; it knows no owner. The others: `worker_list`, from the worker list (below); `worker_entry`, from the worker list, once for each worker whose entry differs; `worker_containers` and `application`, from the code that writes the rows (below); `checks`, for each settings check (it records a new `checked_at`); `latest_version`, when `refresh_latest_release` finds a version that differs; and `release`, whenever a release was fetched (its `fetched_at` is new). A new topic is a loader and its sources; what a topic shows that no source covers stays stale until the next change, so a source is tested to fire when its owner's state changes and not when it does not.
- **The workers** have one marker, `Manager.worker_list`: for each worker its `hostname`, `ip`, `online`, `status`, `path` (direct or relay) and `container_count` and `infra` (the state of each container of its stack, `missing` when the report does not list it) from its current report (none when it has no current report), and `metrics`, its newest minute of metrics (the minute's peaks and the machine's size, while the minute began less than `WORKER_METRICS_STALE_SECONDS` ago), sorted by name. It is derived, not read: the marker keeps one `WorkerInputs` per hostname (`services/manager/decisions.py`: the `ip` of its row, `online`, the worker tasks queued or running for it, the reboot mark, the newest report, the path and the newest minute of metrics), and `worker_entry` and `worker_status` are pure functions of a record and the clock. Each owner pushes only its own fields, through `update_worker(hostname, **inputs)` (several at once with `update_workers`), and the one function that works the list out, `_edit_workers`, applies the change under a lock, derives every entry, replaces the list and fires `worker_list` when it differs. Nothing is read from the worker table, the queue, the reports or Tailscale in it. The owners: `_save_worker` and `_forget_worker` write the Worker table and the record together (they are the only writes of it), `load_workers` reads the table once, at start and after a restore replaced it; `setup_worker` and `set_worker_offline` push `online`; `reboot_worker` pushes the reboot mark; the report route pushes each report it accepts (`Arrival.received`); `refresh_worker_paths` pushes the paths; `flush_worker_metrics` pushes each listed worker's newest minute once a minute, in one change; and `workers_queue_facts` pushes the set of worker tasks, as a `Fact` callback. A record is a worker once it has an `ip`, which only the row writers give it: a push for a name without a row makes a record that is not listed, and the report or task that arrived before the row is there when the row is, while `online` and the reboot mark start over with a new row and a removed worker's record keeps only its tasks. A record with nothing in it is dropped. A status that changes only with the clock (a report going stale after `REPORT_STALE_SECONDS`, the start-up grace, the reboot limit) shows the next time the list is worked out: every push does, so does the end of the 30 s sync (`update_workers({})`), and the path refresh pushes every 60 s, so it is seen within a minute and usually within a report interval. `GET /workers` and the `workers` topic read the marker, and so do `GET /workers/{hostname}` (`infra`, `online`, `status`, `path`, `metrics`) and the daily summary, so they cannot disagree.
- **The rows of applications and containers** say so where they are written, with the key of what they changed. `services/manager/application.py` holds `save_application` and the Manager method `save_container` (the only `save()` of those rows in the manager; a test fails if another appears) write and tell, `container_changed` tells the application's page, the page of the container's worker and the Domains page (`domains_changed`, which the domain routes and a worker's new subdomain call too), `application_changed` the application's, and `hosts_changed` the application's and those of every worker it is on, for what the workers' pages show of an application (its label, its project's). The API's routes call them after a write that took effect: an application created, changed or deleted, a container created or changed, a domain or volume written, a project changed (for each of its applications). `services/manager/domains.py` owns `save_domain` and `domain_changed`: Domain results tell the Domains page and its linked application, and relinking tells both applications. Queue changes publish the same topics for `syncing`. The end of a platform restore tells too. `generic_create`, `generic_update` and `generic_delete` signal nothing, and nothing hooks the database.
- **The rows of backups** say so where they are written: `backups` is fired by the volume backup and the platform backup when each records its row, by discovery and the daily cleanup when they added or removed rows, by `DELETE /backups/{b}` and at the end of a restore. A test fails if a function in the manager or the routes writes a backup row without firing it.
- **Feeds** are sent rows, not told of a change: `Updates().publish(feed, rows)` (rows in the REST route's shape, safe from any thread, nothing before the first connection) sends them to the feed's subscribers without reading anything. `Manager.notify` publishes each stored notification, and `Manager._persist_task` each finished task once it is stored, as `model_to_dict` of the row, which is the shape the list returns. Each published batch is its own frame, in the order published, and a frame is never replaced by a later one. The UI gets the rows a page starts with, and the ones it missed while disconnected, from `GET /notifications` (see frontend.md), and merges by id.
- **What is sent.** After a change the hub waits `UPDATES_COALESCE_SECONDS` (3 s) so a burst is one pass, then computes each topic somebody follows that depends on a source that changed, on `UPDATES_EXECUTOR`, and sends it to its subscribers. A topic is computed one at a time, so an older state never overtakes a newer one, and a change that arrives during a computation is computed once more afterwards. A connection holds only the newest pending frame for each topic (the rows of a feed are the exception, none replaces another), so a slow browser skips states and never queues them, and one that cannot be written to does not hold up the others. A loader that fails is logged and nothing is sent; the topic is computed again at the next change.
- **Heartbeat.** The server pings each connection every `UPDATES_PING_INTERVAL_SECONDS` (10 s) and drops one that does not answer within `UPDATES_PING_TIMEOUT_SECONDS` (10 s). The UI shows each ping as the heartbeat of its indicator.
- **The handshake.** A browser lets any page connect to the manager and CORS does not stop it, so `same_origin` (in `routes/updates.py`) accepts a request only if its `Origin` is the scheme the request arrived with and the host it was sent to; a client that sends no `Origin` is not a page and is accepted. The scheme is the ASGI scope's, with a WebSocket's `ws` and `wss` read as `http` and `https`; uvicorn sets it from `X-Forwarded-Proto` when the proxy is trusted (127.0.0.1), and from the connection itself when it serves TLS directly. engine.io's own default rule, which builds the origin from `X-Forwarded-Proto` alone, refused every WebSocket handshake behind Traefik (it says `wss` for one, where a page's origin says `https`) and would have refused them in production too (no header, so `http`); a proxy in front of the manager has to say how it was reached. In production `OnlyManagerAddress` (pure ASGI, so it sees HTTP and WebSockets) answers only at the manager's address: another name is redirected, or for a WebSocket refused. This adds no authentication; it keeps the exposure of the read API as it was.
- **Access log.** Socket.IO's path (`SOCKET_IO_PATH`, `/api/socket.io`) is left out of the access log, since long-polling is a request every few seconds.
- **Tests.** `tests/routes/test_updates.py` serves the real app with uvicorn, over plain HTTP and with TLS of its own as in production, and connects with `socketio.Client` over long-polling, a WebSocket and the upgrade from one to the other, with the headers of a proxy and without (`websocket-client` is a test requirement). uvicorn 0.34.0 uses `websockets`' legacy implementation, so the tests warn about deprecations until uvicorn is upgraded.

## Orchestration Layer

`app/services/manager/` is a package of mixins composed into the `Manager` singleton, the main backend coordinator. It handles:

- worker enrollment and setup
- worker removal and online/offline transitions
- remote config sync over Tailscale
- deployment and stop flows
- Traefik/domain synchronization
- backup and restore operations
- notifications
- cleanup logic

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
- `services/manager/traefik.py`
  - worker-side Traefik routing-config generation (mixin)
- `services/metrics.py`
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
  - notification formatting and webhook send (the off-thread dispatch is owned by `Manager.notify`)

## Logging And Task Context

The backend relies on `app/utils/logging.py` for task tracing.

Key rules:

- Every line logged inside a queued task carries its `task_id`, and inside a container's part its `sub_task_id`; a request has none. A container's part (its deploy, stop, delete, backup or restore) runs in `container_part` (`services/manager/application.py`), which records its `Event` with a part id of its own.
- `task_id` lives in a `ContextVar`.
- The operation queue's `run_task` sets the `task_id` for each queued task; the offload helpers copy the context so worker threads keep it.
- Each line on stdout shows its task's id (`TaskIdFilter`, `task/part` inside a container's part).
- The access log is uvicorn's, without the requests asked every few seconds or more: a route marks itself, `@router.get(path, openapi_extra=NO_ACCESS_LOG)` (the health check, the log, metrics and task log loads, and the workers' pushes), and Socket.IO's path (`SOCKET_IO_PATH`) is left out too. `main.py` reads the marked paths from the two apps' OpenAPI description, which lists the routes of every router they include, when it sets up the logging (`unlogged_paths`, `setup_logger`). No server turns `access_log` off: the access logger is shared, so that would silence it for all of them.

This is a project-wide pattern, not a local implementation detail.

## Async And Blocking Work

- Local Peewee + SQLite calls run inline (WAL mode; cheap and local).
- Blocking network/remote I/O is offloaded onto a thread pool — never `asyncio.to_thread()`, which runs on the default executor, outside the lane pools. Two helpers in `app/utils/executor.py`:
  - `run_in_executor_with_context(func, *args, executor=None)` — awaited, from async code. Resolves the pool as explicit `executor` → the running task's lane (`active_executor`) → otherwise raises. Returns an awaitable.
  - `submit_with_context(executor, func, *args)` — fire-and-forget, works without a running loop (e.g. `Manager.notify` webhook sends).
- Both `copy_context()` so the worker thread keeps the caller's `ContextVar`s — notably `task_id` for log correlation.

## Operation Queue

The Manager owns a single in-memory `TaskQueue` (`app/utils/queue.py`). It is the one place concurrency is controlled; the scheduler only enqueues.

- **Enqueue.** Every operation runs via `Manager().add_task(task=..., scopes=..., executor=..., params=..., ...)`. Handlers receive ids (not ORM objects) and re-fetch, so nothing carries a stale snapshot across the queue boundary.
- **Scopes** are `:`-delimited and hierarchical. The Manager's roots are declared in `services/manager/queue.py`: `platform`, `app` (with `app:<qualified_name>` children), `domain` (with `domain:<id>` children), `common`, `metrics`. A parent scope conflicts with all of its children; siblings never conflict. Conflicting tasks are mutually excluded. Scope roots and executor lanes are registered independently; a task can require several scopes and use one existing lane. Every operation holding both `platform` and `app` also holds `domain`, including every route, scheduled trigger and restore follow-up for it.
- **Dispatch.** A one-second APScheduler interval tick calls `dispatch_tick`, which starts every pending task whose scope is free. One dispatcher makes the scan race-free; a `threading.Lock` guards the queue because producers run on FastAPI/loop threads. Dispatch is **deliberately opportunistic**: conflicts are checked against *running* work only, so a blocked pending task reserves nothing and later-queued non-conflicting tasks start past it (FIFO holds within a scope, not across conflicting scopes — a queued broad `{platform,app,domain}` task waits for a tick with no app task running, and its start can be pushed out by later app work). This is the accepted trade: a pending-scope reservation rule would let any queued broad task freeze every `app:<qn>` task behind long-running work, converting rare bounded delays on platform ops into fleet-wide stalls. `priority` is queue-position only; nothing ever preempts running work. The scan (`next_to_start`) and admission (`admit`) are pure functions of plain data in `utils/queue.py`, which the queue applies under its lock.
- **Admission policy — `on_conflict` on `add_task`** (enum `OnConflict`; decided atomically under the queue lock). Admission decides *drop vs. enqueue*; mutual exclusion is **separate and always scope-based** — the dispatcher serializes conflicting scopes regardless of mode, so an enqueued task simply waits for its scope to free.
  - `DEDUP` (default) — drop and return `False` if an identical op (**same name + exact scope**) is already running or queued; otherwise enqueue and wait. Prevents reconciler pile-ups and double-triggers (e.g. a second deploy of the same app). A *different* op holding a conflicting scope does **not** cause a drop — it enqueues and waits behind that op. (So a background sync never rejects a user action; the action just defers behind it.)
  - `QUEUE` — always enqueue and wait, even for an identical op. For work where every call must run, including params-identified work that shares a name + scope (S3 delete by path, worker removal by host, the settings Traefik refresh by change-flags).
  - `REPLACE` — cancel any **pending** duplicate (same name + exact scope; latest-wins), then enqueue and wait. Used by the platform backup (cron + UI enqueue identically: at most one backup queued behind a running one) and the worker force-resync.
  - `DEDUP`/`REPLACE` identity is **name + scope, not params**. DEDUP rejecting a params-distinct call is safe (it returns `False`/409, nothing is lost); REPLACE would *silently* drop it, so params-identified work uses `QUEUE`.
- **`priority`** (separate flag) — insert ahead of lower/equal work but behind dominating scopes.
- **`quiet`** (separate flag) — record only on failure (see below).
- **Persistence.** The queue calls a `record` hook (`Manager._persist_task`) at each terminal state. Only `completed`/`failed`/`cancelled` are written to the `Task` table; running and queued tasks live in memory and are exposed via `snapshot()` for the UI. A task keeps when it was queued (`QueuedTask.queued_at`) and when it started (`started_at`, set as the queue starts it); its row keeps them as `created_at` and `started_at` (none for a cancelled task, which never started), beside `finished_at`. `quiet=True` tasks skip the `completed` record, and the `cancelled` record when superseded or drained while queued, so scheduled housekeeping (metrics collection, per-app status sync, per-record domain sync, the 30s worker sync, the settings checks) doesn't flood the table — failures still record. The table is the history of operations worth debugging; the same module reads it (`task_history`, `task_history_filters` in `services/manager/queue.py`, which `routes/tasks.py` answers with). A quiet task also never makes a settings section show as applying. Status vocabulary: `cancelled` means the task **never started** (REPLACE supersession, the drain on stop); a task interrupted **mid-run** — the loop's shutdown cancels its coroutine — records as `failed` (started but did not complete). A forced stop (second signal, or the runtime's kill after its grace period) ends the process with the running task unrecorded.

To make a deliberate user action coalesce with a recurring one, enqueue both with `on_conflict=OnConflict.REPLACE` (latest-wins; e.g. the platform backup and the worker force-resync); no per-operation locks or pending flags are needed.

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
- **Restoring** (a platform restore). Queuing the restore (`add_task(..., restore=True)`,
  refused unless the queue is running) sets the state at once: it behaves like a pause
  except that the restore itself starts, as soon as the running platform, app and domain tasks have
  finished. The UI shows a blocking overlay (`RestoreOverlay`) from `GET /api` — the state
  and `restoring_from`, the backup's timestamp — so a refreshed page shows the same. The
  restore task ends the state with `Manager().end_restore(restored, follow_ups)` in a
  `finally`: when the restore took effect, the tasks still waiting (queued against the old
  data) are recorded as `cancelled`, the follow-ups (worker force-resync, domain reconciliation, backup discovery)
  are queued ahead of anything added since, and the UI reloads; after a failure or a
  rollback the waiting tasks simply run. A stop signal cancels a restore that hasn't
  started; one that is running finishes first.
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
| `LANES["platform"]` | 1 | `platform` lane — main-DB backup/restore, cleanup |
| `LANES["common"]` | 1 | `common` lane — version refresh, daily summary, S3 delete |
| `LANES["app"]` | 6 | `app` lane — per-application deploy/stop/backup/restore |
| `LANES["metrics"]` | 2 | `metrics` lane — collection, metrics-store cleanup |
| `UPDATES_EXECUTOR` | 2 | the pushed topics of the UI's Socket.IO connection (off-queue) |
| `NOTIFICATIONS_EXECUTOR` | 1 | `Manager.notify` webhook sends (off-queue, fire-and-forget) |
| `LOGS_EXECUTOR` | 1 | Workers' Vector log ingestion (off-queue, request-path; single SQLite writer) |

Worker counts are **fixed I/O-concurrency limits, not derived from `os.cpu_count()`** — these lanes run blocking network I/O (Tailscale, S3, httpx), so the manager's core count doesn't gate them, and in a CPU-limited container `os.cpu_count()` reports host cores rather than the container's allowance. `platform` has no child scopes (its tasks never run concurrently) so it stays at 1; `common` is the low-priority lane (settings checks, log cleanup, release refresh) and stays at 1 too, so its tasks run one at a time even where their scopes (`common:<section>`) would allow more; `app`/`metrics` are sized for the realistic ceiling (≤10 workers, ≤50 apps, a couple of parallel deploys). Concurrent DB writes from `app`-lane threads are safe: Peewee keeps per-thread SQLite connections and the DB runs WAL + `busy_timeout`.

A task callable is sync or async, and `run_task` handles both:

- **async task** — awaited on the event loop; it fans blocking leaves out to its lane with `run_in_executor_with_context`. The task coroutine itself never occupies a pool worker.
- **sync task** — run entirely on its lane pool via `run_in_executor_with_context`.

**Ambient executor.** Before running a task, `run_task` binds its lane to the `active_executor` `ContextVar`. Any `run_in_executor_with_context(fn)` inside the task with no explicit `executor=` offloads to that lane, so the lane is declared once at `add_task` and inner call sites don't repeat it. `active_executor` is a `ContextVar`, so concurrent tasks on different lanes never see each other's binding.

**Fan-out is safe because orchestrators are async.** `deploy_application`, `stop_application`, the backup/restore container loops, and the worker cert sync (`sync_certificates_to_workers`) `await asyncio.gather(...)` over offloaded children. The async parent runs on the loop (holds no worker), so children just queue and drain on the lane pool — no parent-starves-child deadlock. A task is marked "running" once its scope is acquired, independent of pool-thread availability, so its offloads may briefly queue inside the pool; that is bounded throughput, not a stall. The invariant: **never make a sync task that submits to its own lane and blocks on the result** — that deadlocks a single-worker lane. (`run_in_executor_with_context` enforces part of this by requiring a running loop; keep any raw `submit` calls fire-and-forget.)

## Choosing A Task Shape

Three shapes are in use; pick by how the units of work relate:

- **One async task that `asyncio.gather`s its offloaded leaves** — for several *independent* remote operations that belong to one logical action and share one scope. The async parent holds no pool worker; the blocking leaves fan out and drain on the task's lane (bounded by its pool size). Use when the work is one operation internally parallelisable across targets: a deploy across its containers, the backup/restore container loops, the cert sync across workers.
- **Per-entity fan-out — one `add_task` per entity** — for *independent* work where each unit deserves its own scope, lifecycle, failure isolation, and DEDUP. The scheduler/orchestrator enqueues one task per entity with a per-entity scope: per-app status sync (`app:<qualified_name>`), per-host metrics collection (`metrics:<host>`), per-app due-volume backups. One slow or failing entity only affects its own task, and each is independently coalesced/rejected.
- **Sequential `await`s in one task** — for *ordered or dependent* steps that must not overlap: stop a container *then* back its volumes up; write a worker's config files *then* `compose up`. Parallelising these would corrupt ordering, so they stay serial even though they hold their scope for the full duration.

The invariant above still governs all three: a fan-out parent must be async (never a sync task blocking on its own lane).

**When adding or changing a task:** pick the shape above, declare its `scopes`, `executor` lane, and `on_conflict` at the single `add_task` call site, and **add a row to the [Task Catalog](operations.md#task-catalog)**. Keep comments and docs describing the *current* behaviour only — never the prior design.

## Data And Persistence

Main database:

- `/app/data/data.db`
- SQLite
- WAL mode enabled
- some fields are encrypted through custom Peewee field types
- its schema follows the models: at boot and after a platform restore, `Database.reconcile_schema` creates the
  missing tables and indexes, adds the missing columns and drops those no model has, in one transaction with foreign
  keys off (SQLite's procedure for changes that rebuild a table, as adding a `NOT NULL` column does). Data that
  refuses the change (a unique index, the foreign key check) rolls all of it back and raises `SchemaRefused`: the
  boot stops with one `[CRITICAL]` line naming what failed and linking the running version's release, whose notes
  give the fix

A Domain keeps its durable sync result in `status` (`pending`, `synced`, `error`), with `sync_error` for failure
detail. `synced_hash` and `synced_at` hold the last fully applied configuration's fingerprint and naive-UTC time;
they are nullable until the first success. A new record starts pending, with no error or successful baseline.
`created_at` and `updated_at` describe row writes; `synced_at` describes a complete successful sync. Running and
queued operations belong to the in-memory task queue.

Additional storage:

- metrics SQLite shards
- the logs, a SQLite file for each kind and for each application under `logs/` (`services/logs/`):
  - each application's shard, `logs/<project>-<app>.db`: its lines as Vector sends them, with a full-text index (FTS5). `GET /projects/{project}/applications/{application}/logs`, with a worker, a search, a window and a `limit` (at most `LIST_MAX_ROWS`, 1000, which is also the default), returns the newest `limit` that match, in the order they were stored, and whether there were more.
  - markers: what Sage did to a container, written when the action ends with its outcome and its task's id (`mark` beside `container_part` in `services/manager/application.py`, `Logs.write_marker`): a line of the container's worker at the manager's time, in a stream of its own, `sage`, written on `LOGS_EXECUTOR`. Deploy, stop, delete, the status sync's stop of a stale version, a backup's stop and start, a volume restore and a worker's reboot each leave one; a marker that cannot be written is a warning, never the action's failure.
  - the search finds the text anywhere in a line, in any case: a `LIKE` on the index, which the `trigram` tokenizer serves for three characters or more, walked newest first and stopped at the cap. A shard made before the tokenizer is scanned.
  - the requests, `logs/requests.db`: Traefik's access lines parsed into start time, worker, host, method, path, status, duration, router, service, client and size, with no full-text index. `GET /requests/`, with a worker, a method, a status class, text in the host or the path, a minimum duration, only those a router matched (`routed`), a window and a `limit` (as the application logs'), returns the newest `limit` that match, newest first, and whether there were more.
  - the task logs, `logs/tasks.db`, with no full-text index: the lines the manager logs inside its queued tasks, read by task. `GET /tasks/{task_id}/logs`, with `sub_task_id` for one container's part, returns every line, in the order they were written; `GET /tasks/{task_id}/parts` lists the parts that logged lines, in the order of their first line, each with its kind and worker from its `Event` (none when the event went with its container).
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

The `settings` part of `GET /api` gives each section (tailscale, cloudflare, s3, notifications) a state: `not_configured`, `applying` (a check or apply task is queued or running, except a quiet one such as the scheduled 5-minute check; its `task_id` is included; which tasks count, and with which params, is `APPLYING_TASKS` in `services/manager/settings.py`, written with `task(name, **params)`, e.g. `task("sync_workers", force=True)`), `ok`, `retrying` (the last check failed in a way that clears on its own — network, timeout, 5xx/429; the 5-minute check retries) or `error` (needs the user — bad credentials, missing bucket or zone). Cloudflare is also `error` for invalid certificates or retained maintenance errors, including failed renewal of a still-valid certificate, and its entry carries `details` for the read-only page: account and tunnel ids (dashboard links), the platform tunnel's health from the 5-minute check (`healthy`, `degraded`, `down`, `inactive`; the check reads every tunnel of the account in one list call and gives each listed worker its own tunnel's, `tunnel` in its worker entry), a certificate inventory (`id`, desired `names`, `valid`, `expiry`, `error`) for the manager, discovery and each worker, including pending retirement. Tailscale and Cloudflare are env vars, so they're always configured and checked as set. Notifications are independent channels (`CHANNELS` in `services/notification.py`, one settings field each): the section is configured once any channel is set, and its check covers every configured channel, reading each webhook without posting. One failing channel fails the section as `error` (`retrying` only when every channel failed in a way that clears by itself), the message names the failing channels, and the status entry's `channels` maps each failing field to its reason so the page can show it beside the field. `POST /settings/{s3|notifications}/test` checks the values in the request (saved or not) with side effects: the S3 test writes and deletes a marker object, the notifications test sends a message. Check failures never include a webhook URL (it holds the token).

Backups live in S3, which is optional: every backup route (create, restore, delete, discover, download) returns 400 until S3 is configured, and the backup triggers skip.

**Downloading a backup** (`GET /backups/{b}/download`, also nested under a volume): the manager checks the object is still in S3 and answers with a presigned GET link that expires after 60 seconds (`BROWSER_LINK_SECONDS` in `services/s3.py`) and asks for the file as an attachment with a name that says which volume it is from. The browser then fetches the file from S3 directly, so no backup passes through the manager. The link is made when asked for, never stored or listed, and S3 checks its expiry when a request starts, so a download already under way isn't cut off. Whoever has a link can read the file until it expires, but the file is encrypted and authenticated with `ENCRYPTION_KEY` (see Settings → S3 for the `openssl` command that opens it). Who may ask for a link is the same as for every other route: whoever the tailnet policy lets reach the manager. Every presigned link states its validity: the links handed to workers for volume backup and restore use `WORKER_LINK_SECONDS` (1200 s; the upload starts after the archive is built, which the script's 900 s command timeout bounds) and reach `curl` through a `--config` file, so they stay out of its command line. The download route is a GET, so it also works while Sage is paused or restoring. The S3 endpoint has to be reachable from the browser, and over https (a browser may refuse to save a file from a plain-http endpoint opened from an https page).

Deploy, application status sync, Domain sync, application backup and `delete_container` re-fetch their target by id with `get_or_none` and treat a missing row as a **quiet no-op**. Instant deletes can interleave with queued work; Domain artifacts retain their record ID for parent cleanup. Stop and volume restore use `get_by_id` and fail on a missing input. `remove_worker` returns for a missing worker, but fails loudly when its precondition re-check finds containers or internal domains. See [consistency boundaries](operations.md#consistency-boundaries).

Container create is an **instant route-side model write**, like most entities' CRUD — not a queued task. The `container_count` signal recomputes atomically from the rows and `only_save_dirty` keeps concurrent status saves from clobbering it, so no serialization is needed; only `delete_container` stays queued (it does remote cleanup).

**Deploy input snapshot.** Config edits (app env/args/image/repo/command/build_secrets/type, project env, volumes) stay instant and unserialized — the contract is that they apply on the **next** deploy, not the running one. `deploy_application` captures a frozen `Application.deploy_config` (a `DeployConfig`) and the container list once, inside a single `db.atomic()` transaction, so one deploy reads a single point-in-time view instead of re-reading each `container.application.*` across the gather. `deployed_at` is set before the snapshot so `config.deploy_stamp` is this deploy's version marker; the child renders entirely from `config` (only `container.worker`/`container.status` stay live). The final status judges **only the snapshot's container list**, so an instant create landing mid-deploy is left for the minutely status sync, not folded into the outcome. Re-validation at snapshot time is part of the freeze: a gone app row is a quiet no-op, and a deploy that finds **zero containers** (a last-container delete that landed first) marks the app `inactive` rather than gathering over nothing and faking `active`. The load-bearing invariant is that `delete_container` stays queued on the app scope, so under a running deploy container rows are only ever *added*, never removed — the snapshot's worker references stay valid for the deploy's duration. Backup/restore already snapshot their container/volume set at task start; their remaining volume-delete / backup-delete races fail loudly by design.

**Config-changed indicator.** Deploy stamps `Application.deployed_hash = config.content_hash` (a SHA-256 over the code-deployment inputs only — build/env/volumes; it deliberately excludes `deploy_stamp`, which is fresh each deploy, and the immutable `qualified_name`). `Application.config_dirty` compares the current `deploy_config.content_hash` against that baseline; the application GET exposes it so the UI shows a "configuration changed since last deploy" hint. A null baseline (never deployed) is not dirty.

**Interrupted-operation recovery.** A busy status (`deploying`/`stopping`/`backup`/`restoring`) is only ever written by a task while it holds that app's scope — and `sync_application_status` holds that same scope while it runs, so by mutual exclusion any busy status it observes has no owning operation left: the mark of an interrupted task (crash, plain container restart, cancelled coroutine). Instead of skipping such apps, the sync resets the stuck app/containers to `error` (with a notification) and and the next sync that has a usable report converges them (the reset is itself a write, which no earlier report can be compared with, so that is at most a minute later). This is why no boot-time status reset exists: the minutely sync covers startup wedges and mid-flight loss with one mechanism. Detection defers while a broad `app`-scope task runs (the per-app syncs queue behind it) and resumes when it finishes. Deploy/stop container children additionally run their whole body inside their try block so an early failure can never kill the parent's gather and leave detached siblings running.

**Worker states.** A worker is online when its SSH server sends its banner through `tailscale nc` and it was set up. This is held in memory, not in the database: it is the `online` of the worker's entry in `Manager.worker_list`, set to true at the end of `setup_worker` and to false by `set_worker_offline` (`update_worker(hostname, online=value)`), and read through `is_worker_online` and `online_workers()`. Until a sync has looked at a worker, at start and after its row is written, `online` is not known (null, status `checking`): the worker is not online for deploys, backups and routing, the status sync decides its containers from its report rather than as out of the pool, and its removal is refused while it has containers. The sync sets it up at its first answer and takes it offline (its DNS record deleted, a notification sent) once it is found offline, so a worker that died while the manager was down is taken out at start. The check is the path commands take (`Tailscale.reachable`, combined by `Tailscale.online_states` and decided by `plan_worker_sync`, which is given whether each worker was online): online at the first answer, offline after two misses in a row (a worker not known yet stays not known until then), and unchanged while the manager's own node is cut off from the tailnet. Neither Tailscale's own `Online` flag nor `tailscale ping` is used: the flag is the coordination server's view and can lag by many minutes in both directions, and a disco ping gives a false offline while a path is cold. Whether the worker is ready is worked out from its report (`worker_ready`): a report less than 30 s old and Traefik's port answering, unknown for a worker that has not reported just after the manager started. A worker out of the pool puts its active containers in `error`; a worker that is in the pool but cannot be asked and has no usable report leaves its containers as they are (`reconcile_container`). A report is trusted for a status decision only when it is current and arrived at least 15 s after the last write to the application's rows; otherwise nothing is known and the containers keep their status until the next report or the next minute's sync. There is no SSH fallback: the worker JSON's `infra` shows the state of the stack's containers from the same report. `sync_workers` ends with `repair_worker_stacks`: when the reports say a stack container is down, Traefik is not answering, or the worker has been silent for a minute, it runs `docker compose up -d` for the stack, at most once a minute per worker and not while the worker is being rebooted or removed, and `setup_worker` runs the same command when nothing drifted. Docker does not retry a container whose start failed, which is what happens to Traefik when a worker boots before its Tailscale address exists.

**Adding and removing a worker.** Workers are added by hand. `POST /workers/` takes a hostname or Tailscale IP, and optionally a tag and a pool, finds the machine in `tailscale status` (`Tailscale.find_peer`, which lists every peer that has an address) and queues `add_worker` on the `platform` scope: it checks that the machine's SSH server answers, then `setup_worker` does the rest, so a failed addition rolls back the same way a failed sync of a new worker does. `sync_workers` only looks at the machines that are already workers, so a deleted worker stays deleted whatever tags its machine carries. Every worker has a subdomain, one DNS label and unique, which its internal names are under (`<name>.<subdomain>.int.<domain>`): the one given when it was added, or a random one (`Worker.random_subdomain`), and `sync_workers` gives a random one to any worker without (`save_worker_subdomain`, which `PUT /workers/{hostname}` uses to change it). A new subdomain renames the worker's internal names, so it resyncs its internal/TCP Domain records and the discovery files, queues `renew_certificates` (only that worker’s leaf changes) and `sync_workers` with `check_stamps` (whose `setup_worker` moves the worker's record); a new worker queues the renewal too. Worker leaf keys and namespaces are independent; discovery is a shared identity. A worker's `revisions/owner` stamp holds the `Instance` id (a random id made on the first boot and kept in the database, so a restored backup is the same instance) and the domain. `setup_worker` reads it with the other stamps before changing anything and refuses a worker that names another instance (`worker_claim_refusal`): it has to be deleted from that Sage first, which leaves its stamp in place for the owner to remove; a worker with no stamp is claimed first, and a claim with an old domain is rewritten. `remove_worker` refuses a worker that has internal domains, linked or not (`DELETE /workers/{hostname}` too), and one that has containers unless it is known to be offline; for an offline one it force-deletes the containers (`delete_container`) first. If the worker's SSH server answers and its stamp is this instance's, it then runs `docker compose down --remove-orphans` and removes the Sage home directory on it, and tells the user when it could not; a worker that does not answer is forgotten with a warning that the VM has to be removed by hand. It then deletes the worker's DNS records (by their comment, `sage-worker-<hostname>`) and its own tunnel (`sage-<domain>-<hostname>`, which `setup_worker` creates; its connections first, since Cloudflare deletes only a tunnel without any), and a tunnel Cloudflare won't delete is left with a warning notification; the platform's tunnel stays. Either way, the manager closes its pooled SSH connection to the worker.

**Worker status.** The worker JSON carries `status` (`Manager.worker_status`, the entry's in `worker_list`) and `online`. In this order: `removing` while `remove_worker` is queued or running for it; `adding` while `add_worker` is; `rebooting` while `reboot_worker` is queued or running and then, through the mark the task leaves in the worker's record, until the worker reports again with a Telegraf container that has started since the request (`is_worker_rebooting`; comparing the worker's own `started_at` before and after needs no clock agreement), or 5 minutes (`WORKER_REBOOT_LIMIT_SECONDS`); `checking` while `online` is not known; `offline` when `online` is false; `not ready` when the worker answers but is not ready (the rest of the stack may still be coming up after a reboot, which is not part of the reboot); otherwise `online`. The UI shows the status as the worker's indicator and enables Restart only for `online` and `not ready`; the routes refuse a reboot in any other status and a second removal while it is removing. The worker JSON also carries `path`, how the last `tailscale ping` reached the worker (`direct`, or `relay <region>` for a DERP relay; null when it did not answer or the output did not say), which `Tailscale` keeps in memory per peer; `refresh_worker_paths` pings the online workers every 60 s for it, apart from the online decision; the UI shows it next to the status.

Worker convergence (`setup_worker`, reached through `sync_workers` and `add_worker`) runs on the **`platform` scope only** — it does not barrier app work, so the 30-second worker poll is never starved by long builds and worker drift is detected (and converged) within ~30s regardless of app activity. This is safe because app tasks already treat workers as unreliable — a worker can vanish mid-exec regardless of scheduling: deploy/stop/delete/backup **fast-fail on an offline-flagged worker**, any per-exec failure lands the container in `error`, and the status syncs converge from there. The residual race — a deploy targeting a worker whose setup is still mid-flight — fails loudly and converges identically, which is the same outcome as deploying to an offline worker (already possible at any time; placement doesn't require `online`). Tasks that mutate fleet-wide state keep the broad `{platform, app, domain}` barrier: `remove_worker` (deletes the row), `reboot_worker` (restarts the machine), `refresh_traefik` (maintains certificates and rewrites worker Traefik config), and the daily `renew_certificates` — the latter holds `{platform, app, domain}` so it also serializes with `setup_worker`'s per-worker cert sync. The manager issues required certificates in-process (`Certs`, via ACME DNS-01), sequentially within the global operation. Worker setup only distributes available material and requests maintenance when a certificate is missing. Traefik watches its dynamic configuration and reloads certificate changes without a restart. Platform database backup/restore and backup discovery hold the same three roots, covering discovery and unlinked record tasks. There is deliberately no per-worker scope dimension; at ≤10 workers it isn't worth the fragility.

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

Domain apply and discovery reconciliation own their failure notifications. A record failure retains its last
successful proof/time, stores the phase and error, requests cleanup/discovery and sends an error notification linked
to Domains before failing the task. Parent failures aggregate inventory, cleanup and publication errors into one
notification per failed pass; other workers still progress and successful Domain proofs remain intact. The queue
records failed tasks, including quiet tasks, in task history. UI notifications use the existing notification feed.

## Traefik Domain Sync

`sync_domain` owns one Domain record's Traefik file and DNS. `request_domain_sync` marks that record pending and
queues the operation with `REPLACE`, under `domain:<id>` plus its linked application's `app:<qn>` scope. The task
re-fetches the record and validates its application binding before any remote work; a changed binding queues a
fresh request with the correct exclusions. Missing rows are quiet no-ops. An interrupted busy container requests
application status recovery. An offline target stays pending and retains the last successful proof.

The task captures an immutable `DomainConfig` inside a local transaction. It renders `domain-<id>.yml` on the
active container's worker, with router/service identity `domain_<id>`, the current host and port, and the
application's Docker name as backend. HTTP names also get a `HEAD /ping` probe answered by Traefik's `ping@internal`
with `Timing-Allow-Origin: *`, `Access-Control-Allow-Origin: *` and `Cache-Control: no-store`; GET reaches the application.
Stopped containers have their record's file removed.
An unlinked record removes its own file from reachable workers. `sync_file` writes through a temporary name and
renames it, and Traefik's file provider reloads the directory.

Routing is prepared before DNS. Each public record owns the exact comment `sage-domain-<id>`, pointing its name at
its worker's tunnel (`<tunnel id>.cfargotunnel.com`). A stopped container keeps DNS to avoid negative caching.
Internal, TCP and unlinked records have no public CNAME under their comment. Grouped DNS sync removes obsolete
names and corrects the requested fields, including proxy mode. Worker wildcard records belong to worker setup.

Domain persists `status` (`pending`, `synced`, `error`), `sync_error`, `synced_hash` and `synced_at` (UTC). Success
means both routing and DNS completed for the captured configuration; its fingerprint includes rendered inputs,
placement, activity and templates. A failure identifies its phase, retains the previous successful proof/time,
and fails the queued task. A result for a configuration edited during I/O cannot certify the new configuration:
the row stays pending and a new request is queued. Running or queued work projects `syncing` through the queue,
without persisting it. Domain writes and queue transitions publish `domains` and the affected application and
worker topics; a relink updates both former and current parents.
Application rows hold no domain progress state; summary counts use Domain status independently of health.

The nullable `Domain.container` foreign key gives each Container its `domains` backref. Container API payloads
include that collection through `routes/containers.py`'s shared serializer, using the Domain projection for
addresses, sync state and binding details. Application and worker details nest these container payloads;
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

Domain edits, deployment/stop outcomes, container-state changes, container deletion, worker subdomain changes and
manual Traefik refresh request the affected records. Worker setup/rejoin requests the parent scan, which selects
record work from the artifacts and current proof. Container creation has no domains to sync.
Each attempt requests `sync_discovery` without awaiting the parent while holding the child's scope.

`sync_discovery` is the parent cleanup, repair and discovery operation. It holds `domain` and uses the app lane,
so record operations cannot overlap it while application work can. Full scans (`repair=True`) run every five
minutes, at startup, after worker setup/rejoin or removal, on Domain create/edit/delete and manual refresh, after a
platform restore, and on container availability or cached worker readiness changes. External requests coalesce
with `REPLACE`, retaining a follow-up when a change arrives during a running pass. A child's completion requests
`repair=False` with `DEDUP`:
cleanup/discovery can observe its result without immediately retrying failed or offline records. A child's
follow-up cannot run with the child or displace a queued full scan. This keeps retry with the scheduled/external
full scan instead of creating a child/parent retry loop.

The parent inventories each online worker's files and revision stamps, plus one list of zone DNS and, for full
repair scans only, one list of account tunnels, before reading Domain rows in a local transaction. Inventory
precedes owners so an artifact materialized during an instant create cannot be misclassified as orphaned. Each
worker inventory command has a 30-second timeout. Failed inventories remain unknown;
they cannot establish a missing file or DNS record. Other workers still progress and a failed parent records its
own error without changing a successful Domain proof.

A full scan requests record work for pending/error results, stale configuration fingerprints, an exact required
file missing (or a stopped linked target still carrying its route), or public DNS drift in name, type, target or
proxy mode. The parent queues children without awaiting them or applying their DNS itself. Busy application or
container states are left to the app-scoped operation and status recovery; they are not evidence for invalidating
proof or reaping a possible working copy. A record invalidated by this scan remains in discovery marked offline.

Only exact `domain-<id>.yml` files and `sage-domain-<id>` DNS comments carry Domain ownership. Absent IDs are
orphans and are removed. Existing records' old placement files are removed only when the current configuration is
synced and its active target's required file is visible; pending/error replacements retain possible working
copies. A synced unlinked record needs no route, so its copies on returning workers are removed. No old-worker
list, tombstone or cleanup column is stored. Files outside the managed identities, including infrastructure and
certificate files, are preserved. The parent maintains discovery DNS alongside orphan comment cleanup, using the
same zone inventory; worker wildcard DNS remains worker-owned.

Discovery lists every service-assigned record with a resolvable host, including unlinked and pending/error records.
Each member retains its pool and has a Boolean availability parameter: `;online` for true, `;online=?0` for false.
A member is online only when its container is active, its worker's cached status is `online`, and its Domain is
synced with a fingerprint matching its current configuration. All-offline services still have a discovery router.
The flag is derived from local state after the remote inventories, alongside the Domain snapshot. The worker
owner's existing readiness policy includes report freshness, Traefik readiness and startup grace. Discovery neither
probes targets nor runs container status reconciliation. Domain sync results and runtime availability remain
separate facts; an active container cannot advertise an unapplied configuration as online.

`ApplicationMixin.save_container` compares persisted active/non-active state before writing and requests the parent
after a successful change across that boundary. Deploy, stop, status reconciliation, backup and restore share this
writer. Worker pushes, row writes/removal/reload and the existing clock refresh check a `worker_discovery` Fact of
the cached ready-worker set, enqueueing only when that set changes. This Fact records which readiness projection
requested publication; it owns no health state. It is checked outside worker locks and never from queue-Fact
callbacks: those hold a Fact lock that enqueueing could re-enter. Readiness changes due to queue intent are picked
up on the next ordinary report or worker refresh. Unchanged reports, paths and metrics do not request publication.
Connectivity transitions also request the parent, since discovery DNS must change even for a not-ready worker.

Every online worker receives `_discovery-<namespace>-<service>.yml`, a router on `discovery.<domain>` (public, `web`)
or `discovery.int.<domain>` (internal/TCP, `websecure`, tailnet only), matching `?service=<service>` and HEAD, GET or
OPTIONS. Its headers middleware supplies `Sage-Endpoints` (names, pools and availability as an RFC 9651 list),
`Cache-Control: private, max-age=30` (`config.DISCOVERY_CACHE_SECONDS`) and CORS. CORS preflight max-age is 300 seconds.
The discovery fingerprint includes availability and the response cache policy. A matching exact file set plus the
template/service `discovery` stamp avoids rewriting files; a missing file is repaired even with a matching stamp.
New files are written before stale
discovery files are removed, and the stamp is committed last. Public discovery DNS uses comment `sage-tunnel` and
internal online-worker addresses use `sage-discovery-<hostname>` with DNS-only A records.

Published availability reflects detection/report lag, queue and worker I/O, and the client's cache lifetime. Rapid
changes coalesce into the latest state. The existing five-minute full pass repairs missed events; availability
triggers use that same inventory/cleanup operation rather than introduce another publisher.

**Revision stamps make stale worker state detectable.** Every worker carries
`{worker_home}/revisions/` — one hash stamp per artifact class, written by
whatever applied the artifact, and deliberately **outside** `traefik/dynamic/`
(which Traefik watches). Expected hashes are computed from current configuration on demand. The stamps:
`infra` — hash of (worker infra template contents, sage version, domain), written
as the **final step** of `setup_worker` so it is a commit marker (an interrupted
setup leaves the old stamp and gets retried); `certs` — a JSON map of independent `worker` and
`discovery` proofs, each folding the leaf-chain fingerprint and certificate-template digest. Applied state is
shown separately as `certificate` and `discovery_certificate` on worker entries; each holds `covered`, `synced`,
`expiry` (only for a valid matching applied proof) and `error`;
`discovery` — the hash of the services the discovery files list (`discovery_hash`), written by `sync_discovery`;
`owner` — `<instance id> <domain>` of the Sage that manages the worker, written first by `setup_worker`, which
another Sage refuses to set up. Each Domain's applied proof lives in its own `synced_hash`.

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

Maintenance first unpublishes unusable identities on online workers, retires obsolete material, issues required
leaves serially, then applies replacements. A worker uses `traefik/certs/managed/{worker|discovery}-<hash>.{pem|key}`
and a stock `tls.certificates` list. Complete pairs precede configuration publication; unwanted managed files are
removed and proofs written last. Both the actual file set and rendered configuration digest are checked even
with matching proofs. Certificate-template changes belong to these proofs, rather than the infrastructure stamp.
Valid material remains published through failed routine renewal. Offline edited workers converge when they return;
offline removed VMs still require manual decommissioning. Shared discovery remains an explicit shared key.

Artifact fingerprints fold in their **template files themselves** (`templates_digest` in
`services/manager/_common.py`, computed once at import because templates ship inside the image). Infrastructure,
certificates, domain routing and discovery have separate digests. Templates pushed unconditionally, such as the revision writer
and the per-application compose/backup/restore scripts, need no digest. Deployment version is the app compose label `sage.deployed_at`
(mirrors `Application.deployed_at`, both written from the same deploy):
`sync_application_status` reads it from the worker's report (the `sage.deployed_at` tag) and
trusts `running` only when the label equals the app's current stamp — **no
label, or no recorded deploy, is no match**. A running container that cannot
prove its version is **stopped, marked `inactive`, and error-notified**
instead of being flipped back into the routing pool. There is deliberately no
migration leniency: after upgrading to a stamped sage version (or restoring an
older database), every unstamped running container is stopped within a minute
and a manual redeploy re-stamps it — routing follows the stopped containers
(their worker's routers go) and recovers on redeploy.

**`setup_worker` is the single stamp-driven convergence.** Every path — new
worker, offline→online rejoin, IP change, force resync — calls the same
idempotent `setup_worker`, and the worker's stamps decide how much work that
is: infra stamp mismatch (or a changed IP, which is rendered into infra
files) → the full file-sync/compose/restart pass; certs mismatch → per-worker
cert sync only. It requests the Domain parent scan once the worker is online, and
always makes the worker's DNS record `*.<subdomain>.int.<domain>` (deleting its records at any other name), asks
for discovery, marks the worker online, and runs existence-level
orphan cleanup: containers/app dirs with no owning Container row (e.g.
force-deleted while the worker was offline) are composed down and removed —
otherwise a zombie runs forever and squats on a future app's container name.
`force=True` means *distrust every stamp*: the `revisions/` dir is deleted
first, so everything mismatches and the worker converges from scratch — a
brand-new worker (no stamps) takes the same path. Matching stamps avoid infrastructure and certificate writes;
the Domain parent scan is requested after setup. Settings → Resync Traefik remains the manual global
routing heal. Orphan cleanup reads the worker's containers/dirs **before** the
owning Container rows: a row is created (instant route write) before any
deploy materializes its app dir, so a dir captured on the worker whose row is
being created concurrently is present in the later read and never reaped
mid-deploy.

`refresh_traefik` (Resync Traefik) rewrites each online worker's
traefik files directly but deliberately does **not** advance their `infra`
stamps — the stamp vouches for every infra file (compose, `.env`, vector),
and refresh only touched the traefik ones; advancing it could falsely certify
a worker that also missed a version upgrade. The stale stamp costs each worker
one redundant full setup at its next convergence (one-shot per settings
change) — the safe direction, since the stamp never claims a worker is
fresher than it is.

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
match below is exact, and the compose service key (= qualified name, same as
`container_name`) is a unique Docker DNS name on `sage_default`. Worker
hostnames are Tailscale's identity, not ours: the one remaining
`CleanCharField` (dashes allowed, still rejected-not-cleaned on violation).
`ValueError` is the house convention for invalid user input — a global
exception handler in `api.py` (plus the generic route handlers, whose broad
`except` would otherwise swallow it) maps it to a 400 carrying the message, so
routes call `clean`/`validate` bare.

## Settings And Traefik Refresh

Cloudflare and Tailscale are configured by required env vars (`DOMAIN`,
`CLOUDFLARE_API_TOKEN`), so changing them is a
restart. On start, `setup_domain` sets up the new domain (zone, tunnel, the manager's
record, the manager's certificate) before serving, the `renew_certificates` startup run
issues the shared discovery and per-worker certificates, and the `check_stamps` worker sync
converges online workers: the `infra` stamp includes the domain and the ids of the
platform's tunnel and of the worker's own (a new account means new tunnels), the `certs` stamp proves the two applied certificate pairs, and each
Domain configuration fingerprint includes its full host. Resources of a previous domain or account stay
in Cloudflare.

The `Setting` table holds S3 and notifications only; every field is optional and set from
the UI. A database, or a restored backup, can hold rows for keys that aren't defined; they
are dropped whenever the settings load (`Settings.load_all`), and `routes/settings.py` only
serves defined keys. UI updates follow one pattern:
validate the merged value, persist via `Settings().set(...)`, reload the consuming service
(`S3().load()`, `Notifications().load_notifications_config()`), then queue a check.

`refresh_traefik` (Resync Traefik) runs inventory maintenance, forces each worker’s own and shared discovery
certificate application, and rewrites Traefik config and `*.<subdomain>.int` records, and requests every Domain record's sync.

## Templates

`app/templates/` contains generated runtime inputs for:

- worker compose files
- worker Traefik config
- worker Vector config
- worker application backup and restore scripts
