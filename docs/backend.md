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

## API Surface

The main API is mounted in `app/api.py` and exposes route groups for:

- `info`
- `backups`
- `settings`
- `projects`
- `workers`
- `notifications`
- `tasks`

Nested routes cover:

- applications
- containers
- domains
- volumes
- volume backups

The ingestion API is separate in `app/api_ingest.py` and serves what the workers push on port 9001: Telegraf's reports at `/api/telegraf/report` (kept in memory by `services/worker_reports.py`, read by the operation queue) and Vector's logs at `/api/vector/logs`.

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
  - Cloudflare DNS and tunnel management, configured from env (`DOMAIN`, `CLOUDFLARE_API_TOKEN`); finds the domain's active zone and, from it, the account that holds it; connects at startup and again on use if that failed
- `services/certs.py`
  - the manager's certificate (`<TS_NAME>.core.<domain>`, never leaves the manager) and the workers' (`*.int.<domain>`): ACME DNS-01 issuance, renewal, the startup retry and the `:443` in-place reload
- `services/tailscale.py`
  - Tailscale status, SSH command execution and SFTP file sync to workers (asyncssh over `tailscale nc`, since the manager's tailscaled runs in userspace mode)
- `services/certs.py`
  - in-process ACME issuance (Let's Encrypt DNS-01) of the wildcard certificate for workers, and PEM ownership; issuance runs in the `renew_certificates` task, never at startup
- `services/manager/traefik.py`
  - worker-side Traefik routing-config generation (mixin)
- `services/metrics.py`
  - metrics (workers' per-minute peaks from Telegraf's reports, written by the queue; the manager's own container in-process). A minute is written to its shard once it has ended; the queries add the minutes still in memory, so the latest reading is at most one report old and the `1m` view is the last two minutes
- `services/worker_reports.py`
  - Telegraf's reports: parsing a batch into snapshots, and the newest snapshot of each worker in memory
- `services/logs.py`
  - per-container log storage and FTS search; the manager's own logs captured in-process
- `services/s3.py`
  - S3 validation, upload, delete, and presigned URL support
- `services/notification.py`
  - notification formatting and webhook send (the off-thread dispatch is owned by `Manager.notify`)

## Logging And Task Context

The backend relies on `app/utils/logging.py` for request and task tracing.

Key rules:

- Every meaningful log line should carry a `task_id`.
- `task_id` lives in a `ContextVar`.
- FastAPI middleware creates a task id per request and returns it in `X-Task-ID`.
- The operation queue's `run_task` sets the `task_id` for each queued task; the offload helpers copy the context so worker threads keep it.

This is a project-wide pattern, not a local implementation detail.

## Async And Blocking Work

- Local Peewee + SQLite calls run inline (WAL mode; cheap and local).
- Blocking network/remote I/O is offloaded onto a thread pool — never `asyncio.to_thread()`, which bypasses context propagation. Two helpers in `app/utils/executor.py`:
  - `run_in_executor_with_context(func, *args, executor=None)` — awaited, from async code. Resolves the pool as explicit `executor` → the running task's lane (`active_executor`) → otherwise raises. Returns an awaitable.
  - `submit_with_context(executor, func, *args)` — fire-and-forget, works without a running loop (e.g. `Manager.notify` webhook sends).
- Both `copy_context()` so the worker thread keeps the caller's `ContextVar`s — notably `task_id` for log correlation.

## Operation Queue

The Manager owns a single in-memory `TaskQueue` (`app/utils/queue.py`). It is the one place concurrency is controlled; the scheduler only enqueues.

- **Enqueue.** Every operation runs via `Manager().add_task(task=..., scopes=..., executor=..., params=..., ...)`. Handlers receive ids (not ORM objects) and re-fetch, so nothing carries a stale snapshot across the queue boundary.
- **Scopes** are `:`-delimited and hierarchical. Roots: `platform`, `app` (with `app:<qualified_name>` children), `common`, `metrics`. A parent scope conflicts with all of its children; siblings never conflict. Conflicting tasks are mutually excluded.
- **Dispatch.** A one-second APScheduler interval tick calls `dispatch_tick`, which starts every pending task whose scope is free. One dispatcher makes the scan race-free; a `threading.Lock` guards the queue because producers run on FastAPI/loop threads. Dispatch is **deliberately opportunistic**: conflicts are checked against *running* work only, so a blocked pending task reserves nothing and later-queued non-conflicting tasks start past it (FIFO holds within a scope, not across conflicting scopes — a queued broad `{platform,app}` task waits for a tick with no app task running, and its start can be pushed out by later app work). This is the accepted trade: a pending-scope reservation rule would let any queued broad task freeze every `app:<qn>` task behind long-running work, converting rare bounded delays on platform ops into fleet-wide stalls. `priority` is queue-position only; nothing ever preempts running work.
- **Admission policy — `on_conflict` on `add_task`** (enum `OnConflict`; decided atomically under the queue lock). Admission decides *drop vs. enqueue*; mutual exclusion is **separate and always scope-based** — the dispatcher serializes conflicting scopes regardless of mode, so an enqueued task simply waits for its scope to free.
  - `DEDUP` (default) — drop and return `False` if an identical op (**same name + exact scope**) is already running or queued; otherwise enqueue and wait. Prevents reconciler pile-ups and double-triggers (e.g. a second deploy of the same app). A *different* op holding a conflicting scope does **not** cause a drop — it enqueues and waits behind that op. (So a background sync never rejects a user action; the action just defers behind it.)
  - `QUEUE` — always enqueue and wait, even for an identical op. For work where every call must run, including params-identified work that shares a name + scope (S3 delete by path, worker removal by host, the settings Traefik refresh by change-flags).
  - `REPLACE` — cancel any **pending** duplicate (same name + exact scope; latest-wins), then enqueue and wait. Used by the platform backup (cron + UI enqueue identically: at most one backup queued behind a running one) and the worker force-resync.
  - `DEDUP`/`REPLACE` identity is **name + scope, not params**. DEDUP rejecting a params-distinct call is safe (it returns `False`/409, nothing is lost); REPLACE would *silently* drop it, so params-identified work uses `QUEUE`.
- **`priority`** (separate flag) — insert ahead of lower/equal work but behind dominating scopes.
- **`quiet`** (separate flag) — record only on failure (see below).
- **Persistence.** The queue calls a `record` hook (`Manager._persist_task`) at each terminal state. Only `completed`/`failed`/`cancelled` are written to the `Task` table; running and queued tasks live in memory and are exposed via `snapshot()` for the UI. `quiet=True` tasks skip the `completed` record, and the `cancelled` record when superseded or drained while queued, so scheduled housekeeping (metrics collection, per-app status/domain sync, the 30s worker sync, the settings checks) doesn't flood the table — failures still record. The table is the history of operations worth debugging. A quiet task also never makes a settings section show as applying. Status vocabulary: `cancelled` means the task **never started** (REPLACE supersession, the drain on stop); a task interrupted **mid-run** — the loop's shutdown cancels its coroutine — records as `failed` (started but did not complete). A forced stop (second signal, or the runtime's kill after its grace period) ends the process with the running task unrecorded.

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
  except that the restore itself starts, as soon as the running platform and app tasks have
  finished. The UI shows a blocking overlay (`RestoreOverlay`) from `GET /api` — the state
  and `restoring_from`, the backup's timestamp — so a refreshed page shows the same. The
  restore task ends the state with `Manager().end_restore(restored, follow_ups)` in a
  `finally`: when the restore took effect, the tasks still waiting (queued against the old
  data) are recorded as `cancelled`, the follow-ups (worker force-resync, backup discovery)
  are queued ahead of anything added since, and the UI reloads; after a failure or a
  rollback the waiting tasks simply run. A stop signal cancels a restore that hasn't
  started; one that is running finishes first.
- **Stop signal** (`app/main.py`). The first SIGTERM/SIGINT shuts the scheduler down and
  stops the queue: queued tasks, and any added later (e.g. a follow-up queued by a
  finishing task), are recorded as `cancelled`; running ones finish, then the API server
  stops. There is no cap in Sage: the runtime's stop grace period bounds it. A second
  signal stops right away. The ingest server (`api_ingest`) doesn't handle signals, so
  worker logs keep arriving until the process exits.

`GET /api` reports that `state`, the `workers` summary (`total` and `online`, kept in memory by `refresh_worker_summary` after each `sync_workers` run, worker removal and restore, and read from the database once at startup) and the `settings` status. It reads memory only (the certificate expiry too: `Certificate.load` reads the PEM at startup and after each write), because the pause banner and the restore overlay depend on it while the database is being replaced; if building the settings status fails, that part is `null` and the rest is still returned. While it isn't `running`,
`api.py` refuses changes with 503: every method but GET/HEAD/OPTIONS, except pause/resume
and the POST routes that only read (`_READ_ONLY_POSTS`). A new read-only POST route has to
be added there.

## Execution Modes And Pools

Pools (lanes) are defined in `app/utils/executor.py`. The first four are queue lanes wired into the `TaskQueue`; the rest are off-queue.

| Pool | Workers | Used by |
| --- | --- | --- |
| `PLATFORM_EXECUTOR` | 1 | `platform` lane — main-DB backup/restore, cleanup |
| `COMMON_EXECUTOR` | 1 | `common` lane — version refresh, daily summary, S3 delete |
| `APP_EXECUTOR` | 6 | `app` lane — per-application deploy/stop/backup/restore |
| `METRICS_EXECUTOR` | 2 | `metrics` lane — collection, metrics-store cleanup |
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

**When adding or changing a task:** pick the shape above, declare its `scopes`, `executor` lane, and `on_conflict` at the single `add_task` call site, and **add a row to the Task Catalog** (above). Keep comments and docs describing the *current* behaviour only — never the prior design.

## Data And Persistence

Main database:

- `/app/data/data.db`
- SQLite
- WAL mode enabled
- some fields are encrypted through custom Peewee field types

Additional storage:

- metrics SQLite shards
- log SQLite shards with FTS5 (worker containers via Vector ingestion; the manager's own logs written in-process by a logging handler)
- on each worker, Traefik config and revision stamps under `/opt/sage`

## Scheduler Notes

`app/scheduler.py` holds async APScheduler triggers that act only as cron/interval triggers — each one calls `Manager().add_task(...)` and contains no execution logic. Sub-minute pumps (`dispatch_tick`, `sync_workers`) use `IntervalTrigger`; everything else uses `CronTrigger.from_crontab` and fires on exact wall-clock instants. The triggers in `_AT_STARTUP` (`refresh_latest_release`, `renew_certificates`, `discover_backups`, `check_settings`, `check_worker_stamps`) also run once right after startup, so startup network work happens in queued tasks while the servers are already serving. Triggers for optional services skip quietly when the service isn't configured: `backup_database` and `discover_backups` need S3, `renew_certificates` needs Cloudflare. Note: APScheduler's `from_crontab` treats numeric day-of-week `0` as Monday (whereas the `croniter`-matched volume-backup crons use `0` = Sunday) — be explicit about the intended weekday. `dispatch_tick` must stay an `async def` (APScheduler runs coroutine jobs on the loop; a sync job would run in a worker thread where its `asyncio.create_task` has no running loop). Route handlers enqueue through the same `add_task` path, and a few tasks are enqueued internally from within another task.

## Task Catalog

Every queued operation, its scope, where it is enqueued from, its lane pool, and its `on_conflict` policy. Keep this current when adding, removing, or re-scoping a task. `<qn>` is an application's `qualified_name`; `<host>` a worker hostname.

**Scheduled (APScheduler cron/interval → `add_task`)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `sync_workers` | platform | interval 30s | platform | DEDUP |
| `refresh_worker_paths` | common | interval 30s + once at startup | common | DEDUP — pings the online workers to record whether each is reached direct or through a relay |
| `sync_application_status` | app:`<qn>` | cron 1m (via `request_application_status_sync`) | app | REPLACE — decides from the worker's report when it can be trusted, otherwise changes nothing |
| `backup_application_s3` | app:`<qn>` | cron 1m (due backups; skipped when S3 isn't configured) | app | DEDUP — worker `backup.sh`: tar → `openssl enc` (ENCRYPTION_KEY) → HMAC-SHA256 appended (encrypt-then-MAC) → presigned PUT |
| `reconcile_traefik_configs` | platform | cron 1m | platform | DEDUP |
| `Metrics.flush_workers` | metrics:`workers` | cron 1m | metrics | DEDUP — writes the minutes of the workers' reports that have completed |
| `Metrics.collect_self` | metrics:`<manager>` | cron 1m | metrics | DEDUP |
| `send_summary_notification` | common | cron daily 08:00 | common | REPLACE |
| `refresh_latest_release` | common | cron 4h + once at startup | common | REPLACE — caches latest-release detail (version + notes + `fetched_at`) served by `GET /info/release` |
| `check_<section>` (`check_setting`) | common:`<section>` | cron 5m + once at startup, per configured section (quiet) | common | REPLACE — checks the saved settings against the provider without side effects and records the result for the `settings` status in `GET /api` (`services/manager/settings.py`) |
| `backup_database_s3` | platform, app | cron 12h (skipped when S3 isn't configured) | platform | REPLACE — SQLite snapshot, encrypted as a whole in the volume-archive format (`<ts>.db.enc`; `utils/archive.py`: `openssl enc`, then an HMAC-SHA256 trailer; streamed, so memory stays flat) |
| `Metrics.cleanup` | metrics | cron daily 04:00 | metrics | REPLACE — everything past `RETENTION_DAYS` (14, `utils/common.py`), like the two below |
| `Logs.cleanup` | common | cron daily 04:00 | common | REPLACE |
| `cleanup` | platform | cron daily 04:00 | platform | REPLACE |
| `renew_certificates` | platform, app | cron daily 03:00 + once at startup | platform | REPLACE — both certificates; reloads the manager's `:443` in place and syncs the workers' to stale workers |
| `sync_workers` (`check_stamps`) | platform | once at startup | platform | REPLACE — also sets up workers that stayed online, letting their stamps (domain, tunnel, certificate, app routing) decide what to redo, since the env may have changed while Sage was stopped |
| `discover_s3_backups` | platform, app | once at startup (skipped when S3 isn't configured) | platform | DEDUP — makes the Backup table match the bucket under the configured path: records the platform backups and the volume archives whose application exists here (by the project and application names in the key), and drops records whose object is gone |

**Route-driven (HTTP → `add_task`)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `deploy_application` | app:`<qn>` | POST …/deploy | app | DEDUP |
| `stop_application` | app:`<qn>` | POST …/stop | app | DEDUP |
| `delete_container` | app:`<qn>` | DELETE …/containers/{c} | app | QUEUE (route rejects a duplicate delete of the same container via `has_task`, the params-aware pending/running check) |
| `backup_application_s3` | app:`<qn>` | POST …/volumes/{v}/backups | app | DEDUP |
| `restore_application_volume_from_s3` | app:`<qn>` | POST …/backups/{b}/restore | app | DEDUP — worker `restore.sh` checks the HMAC before decrypting |
| `backup_database_s3` | platform, app | POST /backups | platform | REPLACE |
| `restore_database_from_s3` | platform, app | POST /backups/{b}/restore | platform | DEDUP, `restore=True` (see Pause And Stop: the queue is `restoring` from the request until the task ends) — decrypts first (another key or an unencrypted backup is refused before the live DB is touched); safety copy and restore through SQLite's backup API; then reloads the settings and the services built from them (S3, notifications, Cloudflare, certs). The S3 settings are written back to their values from before the restore: they are how this manager reaches its backups, so a backup never changes them. A settings reload failure still rolls back to the safety copy |
| `delete_backup_s3` | common | DELETE /backups/{b} | common | QUEUE |
| `discover_s3_backups` | platform, app | POST /backups/discover; PUT /settings/s3 | platform | DEDUP |
| `add_worker` | platform | POST /workers/ | platform | QUEUE — admission ignores params, so QUEUE keeps one worker's addition from dropping another's; pings the machine, then `setup_worker` |
| `remove_worker` | platform, app | DELETE /workers/{h} | platform | QUEUE — cleans up a worker that answers and is this Sage's, force-deletes the containers of one that is offline |
| `reboot_worker` | platform, app | POST /workers/{h}/reboot | platform | QUEUE — backgrounds a `reboot` over Tailscale SSH so the command returns before the host drops; won't run mid-deploy; status reconciler tracks the worker going offline/back |
| `refresh_traefik` | platform, app | POST /settings/resync_traefik | platform | DEDUP |
| `refresh_latest_release` | common | POST /info/release/refresh | common | REPLACE — on-demand version of the 4h refresh; UI polls `GET /info/release`'s `fetched_at` |
| `check_<section>` (`check_setting`) | common:`<section>` | PUT /settings/{section} (after a save, when complete) | common | REPLACE |
| `sync_workers` | platform | POST /settings/resync_workers | platform | REPLACE |
| `sync_application_traefik_domains_config` | app:`<qn>` | create/update/delete_domain, update-container tag change (via `request_application_traefik_sync`) | app | REPLACE |

**Settings status.** The `settings` part of `GET /api` gives each section (tailscale, cloudflare, s3, notifications) a state: `not_configured`, `applying` (a check or apply task is queued or running, except a quiet one such as the scheduled 5-minute check; its `task_id` is included; which tasks count, and with which params, is `APPLYING_TASKS` in `services/manager/settings.py`, written with `task(name, **params)`, e.g. `task("sync_workers", force=True)`), `ok`, `retrying` (the last check failed in a way that clears on its own — network, timeout, 5xx/429; the 5-minute check retries) or `error` (needs the user — bad credentials, missing bucket or zone). Cloudflare is also `error` without a valid certificate, and its entry carries `details` for the read-only page: account and tunnel ids (dashboard links), the tunnel's health from the 5-minute check (`healthy`, `degraded`, `down`, `inactive`) and both certificates' expiry. Tailscale and Cloudflare are env vars, so they're always configured and checked as set. Notifications are independent channels (`CHANNELS` in `services/notification.py`, one settings field each): the section is configured once any channel is set, and its check covers every configured channel, reading each webhook without posting. One failing channel fails the section as `error` (`retrying` only when every channel failed in a way that clears by itself), the message names the failing channels, and the status entry's `channels` maps each failing field to its reason so the page can show it beside the field. `POST /settings/{s3|notifications}/test` checks the values in the request (saved or not) with side effects: the S3 test writes and deletes a marker object, the notifications test sends a message. Check failures never include a webhook URL (it holds the token).

Backups live in S3, which is optional: every backup route (create, restore, delete, discover, download) returns 400 until S3 is configured, and the backup triggers skip.

**Downloading a backup** (`POST /backups/{b}/download`, also nested under a volume): the manager checks the object is still in S3 and answers with a presigned GET link that expires after 60 seconds (`BROWSER_LINK_SECONDS` in `services/s3.py`) and asks for the file as an attachment with a name that says which volume it is from. The browser then fetches the file from S3 directly, so no backup passes through the manager. The link is made when asked for, never stored or listed, and S3 checks its expiry when a request starts, so a download already under way isn't cut off. Whoever has a link can read the file until it expires, but the file is encrypted and authenticated with `ENCRYPTION_KEY` (see Settings → S3 for the `openssl` command that opens it). Who may ask for a link is the same as for every other route: whoever the tailnet policy lets reach the manager. Every presigned link states its validity: the links handed to workers for volume backup and restore use `WORKER_LINK_SECONDS` (1200 s; the upload starts after the archive is built, which the script's 900 s command timeout bounds) and reach `curl` through a `--config` file, so they stay out of its command line. The download route is a read-only POST (`_READ_ONLY_POSTS`), so it also works while Sage is paused or restoring. The S3 endpoint has to be reachable from the browser, and over https (a browser may refuse to save a file from a plain-http endpoint opened from an https page).

Per-app tasks (status sync, the Traefik domains sync, backups, `delete_container`) re-fetch their target by id with `get_or_none` and treat a missing row as a **quiet no-op**: instant deletes are a legitimate interleaving with queued/scheduled work, so a task whose target vanished has nothing to do (the reconcile scan owns worker-side cleanup for deleted apps). `remove_worker` is the deliberate exception — its precondition re-check fails loudly because proceeding would destroy a worker that just gained containers.

Container create and tag-update are **instant route-side model writes**, like every other entity's CRUD — not queued tasks. The `container_count` signal recomputes atomically from the rows and `only_save_dirty` keeps concurrent status saves from clobbering it, so no serialization is needed; only `delete_container` stays queued (it does remote cleanup).

**Deploy input snapshot.** Config edits (app env/args/image/repo/command/build_secrets/type, project env, volumes) stay instant and unserialized — the contract is that they apply on the **next** deploy, not the running one. `deploy_application` captures a frozen `Application.deploy_config` (a `DeployConfig`) and the container list once, inside a single `db.atomic()` transaction, so one deploy reads a single point-in-time view instead of re-reading each `container.application.*` across the gather. `deployed_at` is set before the snapshot so `config.deploy_stamp` is this deploy's version marker; the child renders entirely from `config` (only `container.worker`/`container.status` stay live). The final status judges **only the snapshot's container list**, so an instant create landing mid-deploy is left for the minutely status sync, not folded into the outcome. Re-validation at snapshot time is part of the freeze: a gone app row is a quiet no-op, and a deploy that finds **zero containers** (a last-container delete that landed first) marks the app `inactive` rather than gathering over nothing and faking `active`. The load-bearing invariant is that `delete_container` stays queued on the app scope, so under a running deploy container rows are only ever *added*, never removed — the snapshot's worker references stay valid for the deploy's duration. Backup/restore already snapshot their container/volume set at task start; their remaining volume-delete / backup-delete races fail loudly by design.

**Config-changed indicator.** Deploy stamps `Application.deployed_hash = config.content_hash` (a SHA-256 over the code-deployment inputs only — build/env/volumes; it deliberately excludes `deploy_stamp`, which is fresh each deploy, and the immutable `qualified_name`). `Application.config_dirty` compares the current `deploy_config.content_hash` against that baseline; the application GET exposes it so the UI shows a "configuration changed since last deploy" hint. A null baseline (never deployed) is not dirty.

**Internal (enqueued from within another task)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `sync_workers` | platform | `restore_database_from_s3` (via `end_restore`, priority) | platform | REPLACE |
| `discover_s3_backups` | platform, app | `restore_database_from_s3` (via `end_restore`, priority; the restored Backup table is the snapshot's) | platform | DEDUP |
| `sync_application_status` | app:`<qn>` | the cron (every application, every minute), and a report that changes a container of the application (`request_status_sync_for_containers`, from the report route) | app | REPLACE — a request that arrives while one runs is kept, since the running one may have read its input before the change |
| `sync_application_traefik_domains_config` | app:`<qn>` | deploy/stop, delete-container, `sync_application_status`, `sync_workers`, `refresh_traefik`, `reconcile_traefik_configs` missing-file check (via `request_application_traefik_sync`) | app | REPLACE |
| `sync_application_traefik_domains_config` | app:`<qn>` | `reconcile_traefik_configs` `domains_synced=False` backstop | app | DEDUP |

Quiet tasks (recorded only on failure): `sync_workers` (cron), `sync_application_status`, `sync_application_traefik_domains_config`, `reconcile_traefik_configs`, `Metrics.flush_workers`, `Metrics.collect_self`, `Metrics.cleanup`, `Logs.cleanup`.

Not every periodic/background job is a queue task. Two service-owned daemon threads run off the queue: the `Metrics` sampler reads the manager's **own** container metrics (cgroup v2 / root-fs / net) every few seconds into an in-memory per-minute peak accumulator; and the `Logs` capture thread drains formatted self-log records (enqueued by a root-logger handler) to the manager's `sage` log shard. Both are trivial in-process work with no scope to serialize (like the `dispatch_tick` pump). Their periodic DB maintenance stays on the queue: `Metrics.collect_self` (minutely peak flush) and `Metrics.cleanup` (daily, fast row-deletes) on the `metrics` scope, and `Logs.cleanup` on `common` — kept off `metrics` because its per-shard FTS `optimize` can run long, and blocking the `metrics` scope that long would stall the minutely flush past the sampler's 10-minute peak buffer. Workers push: Telegraf's reports reach `Metrics.ingest` (in-memory per-minute peaks, written by `Metrics.flush_workers` on the queue), and Vector's container logs go to `LOGS_EXECUTOR`, both via the `:9001` ingestion API.

Cron cadence convention: **≤ 1m → `DEDUP`** (a high-frequency reconciler skips only if its own previous run is still in flight; the next tick retries), **> 1m → `REPLACE`** (a 6h/1d/10d task must not skip its whole cycle on a transient conflict; latest-wins keeps no backlog).

`sync_workers` deliberately differs by source — cron uses `DEDUP`, while resync/restore use `REPLACE` with `{force: True}` to supersede a pending plain sync. That is why DEDUP/REPLACE identity is name + scope and not params.

**Interrupted-operation recovery.** A busy status (`deploying`/`stopping`/`backup`/`restoring`) is only ever written by a task while it holds that app's scope — and `sync_application_status` holds that same scope while it runs, so by mutual exclusion any busy status it observes has no owning operation left: the mark of an interrupted task (crash, plain container restart, cancelled coroutine). Instead of skipping such apps, the sync resets the stuck app/containers to `error` (with a notification) and and the next sync that has a usable report converges them (the reset is itself a write, which no earlier report can be compared with, so that is at most a minute later). This is why no boot-time status reset exists: the minutely sync covers startup wedges and mid-flight loss with one mechanism. Detection defers while a broad `app`-scope task runs (the per-app syncs queue behind it) and resumes when it finishes. Deploy/stop container children additionally run their whole body inside their try block so an early failure can never kill the parent's gather and leave detached siblings running.

**Worker states.** `Worker.online` is whether the worker's SSH server sends its banner through `tailscale nc`, the path commands take (`Tailscale.reachable`, combined by `Tailscale.online_states` and decided by `plan_worker_sync`): online at the first answer, offline after two misses in a row, and unchanged while the manager's own node is cut off from the tailnet. Neither Tailscale's own `Online` flag nor `tailscale ping` is used: the flag is the coordination server's view and can lag by many minutes in both directions, and a disco ping gives a false offline while a path is cold. Whether the worker is ready is read, not stored (`WorkerReports.ready`): a report less than 30 s old and Traefik's port answering, unknown for a worker that has not reported just after the manager started. A worker out of the pool puts its active containers in `error`; a worker that is in the pool but cannot be asked and has no usable report leaves its containers as they are (`reconcile_container`). A report is trusted for a status decision only when it is current and arrived at least 15 s after the last write to the application's rows; otherwise nothing is known and the containers keep their status until the next report or the next minute's sync. There is no SSH fallback: the worker JSON's `infra` shows the state of the stack's containers from the same report. `sync_workers` ends with `repair_worker_stacks`: when the reports say a stack container is down, Traefik is not answering, or the worker has been silent for a minute, it runs `docker compose up -d` for the stack, at most once a minute per worker and not while the worker is being rebooted or removed, and `setup_worker` runs the same command when nothing drifted. Docker does not retry a container whose start failed, which is what happens to Traefik when a worker boots before its Tailscale address exists.

**Adding and removing a worker.** Workers are added by hand. `POST /workers/` takes a hostname or Tailscale IP, finds the machine in `tailscale status` (`Tailscale.find_peer`, which lists every peer that has an address) and queues `add_worker` on the `platform` scope: it checks that the machine's SSH server answers, then `setup_worker` does the rest, so a failed addition rolls back the same way a failed sync of a new worker does. `sync_workers` only looks at the machines that are already workers, so a deleted worker stays deleted whatever tags its machine carries. A worker's `revisions/owner` stamp holds the `Instance` id (a random id made on the first boot and kept in the database, so a restored backup is the same instance) and the domain. `setup_worker` reads it with the other stamps before changing anything and refuses a worker that names another instance (`worker_claim_refusal`): it has to be deleted from that Sage first, which leaves its stamp in place for the owner to remove; a worker with no stamp is claimed first, and a claim with an old domain is rewritten. `remove_worker` refuses a worker that is online and has containers; for an offline one it force-deletes the containers (`delete_container`) first. If the worker's SSH server answers and its stamp is this instance's, it then runs `docker compose down --remove-orphans` and removes the Sage home directory on it, and tells the user when it could not; a worker that does not answer is forgotten with a warning that the VM has to be removed by hand. Either way, the manager closes its pooled SSH connection to the worker.

**Worker status.** The worker JSON carries `status` (`Manager.worker_status`) next to the stored `online`, derived on every call and stored nowhere. In this order: `removing` while `remove_worker` is queued or running for it; `adding` while `add_worker` is; `rebooting` while `reboot_worker` is queued or running and then, through a small in-memory mark the task leaves (`Manager.worker_reboots`), until the worker reports again with a Telegraf container that has started since the request (`is_worker_rebooting`; comparing the worker's own `started_at` before and after needs no clock agreement), or 5 minutes (`WORKER_REBOOT_LIMIT_SECONDS`); `offline` when `online` is false; `not ready` when the worker answers but is not ready (the rest of the stack may still be coming up after a reboot, which is not part of the reboot); otherwise `online`. `sync_workers` forgets the marks that are over. The UI shows the status as the worker's indicator and enables Restart only for `online` and `not ready`; the routes refuse a reboot in any other status and a second removal while it is removing. The worker JSON also carries `path`, how the last `tailscale ping` reached the worker (`direct`, or `relay <region>` for a DERP relay; null when it did not answer or the output did not say), which `Tailscale` keeps in memory per peer; `refresh_worker_paths` pings the online workers every 30 s for it, apart from the online decision; the UI shows it next to the status.

Worker convergence (`setup_worker`, reached through `sync_workers` and `add_worker`) runs on the **`platform` scope only** — it does not barrier app work, so the 30-second worker poll is never starved by long builds and worker drift is detected (and converged) within ~30s regardless of app activity. This is safe because app tasks already treat workers as unreliable — a worker can vanish mid-exec regardless of scheduling: deploy/stop/delete/backup **fast-fail on an offline-flagged worker**, any per-exec failure lands the container in `error`, and the status syncs converge from there. The residual race — a deploy targeting a worker whose setup is still mid-flight — fails loudly and converges identically, which is the same outcome as deploying to an offline worker (already possible at any time; placement doesn't require `online`). Tasks that mutate fleet-wide state keep the broad `{platform, app}` barrier: `remove_worker` (deletes the row), `refresh_traefik` (re-issues the cert and rewrites worker traefik config on a domain change), platform backup/restore, and the daily `renew_certificates` — the latter holds `{platform, app}` so it also serializes with `setup_worker`'s per-worker cert sync. The manager now issues the wildcard cert in-process (`Certs`, via ACME DNS-01) before the sync runs, so there is no wait-for-provisioning spin; the cert reaches a worker as PEM plus a re-written `traefik/dynamic/certs.yml`, whose file-provider `watch` reloads it with no traefik restart. There is deliberately no per-worker scope dimension; at ≤10 workers it isn't worth the fragility.

## Traefik Domain Sync

`sync_application_traefik_domains_config` renders an application's full routing view
(its domains × **active** containers × tag pools) and writes it to every online
worker. It is **disruptive** — it `rm`s the app's `*.yml` on all workers before
rewriting — an accepted property: a triggered sync may briefly gap routing.

**Empty pools are written deliberately.** An application with domains keeps its
per-domain files even with zero active containers (`servers: [ ]` → 503): the
main file carries the `GET /x-tag` discovery route and the `X-Tag` header of
declared tags, so discovery keeps working and the domain stays claimed while
the app is stopped; declared tags likewise keep their (possibly empty) pool
files. Cleanup after **deletion** is owned by the reconciler below — files
outlive their application by at most about a minute.

The single change-trigger is `Manager().request_application_traefik_sync(application)`
(`TraefikMixin`): it marks `domains_synced=False` and enqueues the app-scoped sync
with `REPLACE` (coalesces bursts; the app scope serializes it after any in-flight
app op, so it re-reads committed state). It is called wherever the **active routing
set** changes — domain CRUD, deploy/stop, delete/tag-update of an **active**
container, `sync_application_status` health flips, and Resync Traefik.
It is deliberately **not** called when routing is unaffected: adding a container
(inactive until deployed), or editing/deleting an inactive one.

**Revision stamps make stale worker state detectable.** Every worker carries
`{worker_home}/revisions/` — one hash stamp per artifact class, written by
whatever applied the artifact, and deliberately **outside** `traefik/dynamic/`
(which Traefik watches and the reconcile scan owns). All hashes are computed
from current truth on demand; nothing is persisted manager-side. The stamps:
`infra` — hash of (worker infra template contents, sage version, domain), written
as the **final step** of `setup_worker` so it is a commit marker (an interrupted
setup leaves the old stamp and gets retried); `certs` — hash of the manager's
issued PEM cert chain, stamped by the per-worker cert sync; one per application —
`routing_input_hash` over the per-app routing template contents, domains, tags,
and the active container set (the receiving-worker set is deliberately excluded,
so one worker's outage never invalidates the others' stamps), stamped by the
domains sync on each worker it wrote to; `owner` — `<instance id> <domain>` of the
Sage that manages the worker, written first by `setup_worker`, which another Sage
refuses to set up. The reconcile removes the stamps of deleted applications and
leaves `infra`, `certs` and `owner`.

Both hashes fold in a digest of the **template files themselves**
(`templates_digest` in `services/manager/_common.py`, computed once at import
since templates ship inside the image and cannot change under a running manager).
Without it, editing a template moved no hash, so `setup_worker` skipped the sync
and workers kept the old file until some unrelated input happened to change —
convergence that every other input gets automatically. Two sets are hashed
separately so an infra edit and a routing edit each invalidate only their own
stamps: the infra set is what `setup_worker` pushes behind the `infra` stamp
(`docker-compose.yml`, `worker.env`, `traefik/traefik.yml`, `traefik/config.yml`,
`traefik/certs.yml`, `vector/vector.yml`), the routing set is the
`service_*.yml` pair-per-type that the domains sync renders. Templates pushed
unconditionally on every run — the revision-stamp writer, and the
per-application compose/backup/restore scripts — are excluded by design: they
cannot drift, so hashing them would only cause spurious resyncs. **Editing a
template in either set means every worker (or every app) resyncs once on the
next deploy** — that is the intended repair, and it is the mechanism that would
have caught the retired `certResolver` block surviving on migrating workers. The fourth stamp is the app compose label `sage.deployed_at`
(mirrors `Application.deployed_at`, both written from the same deploy):
`sync_application_status` reads it from the worker's report (the `sage.deployed_at` tag) and
trusts `running` only when the label equals the app's current stamp — **no
label, or no recorded deploy, is no match**. A running container that cannot
prove its version is **stopped, marked `inactive`, and error-notified**
instead of being flipped back into the routing pool. There is deliberately no
migration leniency: after upgrading to a stamped sage version (or restoring an
older database), every unstamped running container is stopped within a minute
and a manual redeploy re-stamps it — routing follows the stopped containers
(pools re-render without them) and recovers on redeploy.

**`setup_worker` is the single stamp-driven convergence.** Every path — new
worker, offline→online rejoin, IP change, force resync — calls the same
idempotent `setup_worker`, and the worker's stamps decide how much work that
is: infra stamp mismatch (or a changed IP, which is rendered into infra
files) → the full file-sync/compose/restart pass; certs mismatch → per-worker
cert sync only; per-app mismatches → targeted
`request_application_traefik_sync` (covers hosted and mesh files alike). It
always re-creates the DNS record, flips `online`, and runs existence-level
orphan cleanup: containers/app dirs with no owning Container row (e.g.
force-deleted while the worker was offline) are composed down and removed —
otherwise a zombie runs forever and squats on a future app's container name.
`force=True` means *distrust every stamp*: the `revisions/` dir is deleted
first, so everything mismatches and the worker converges from scratch — a
brand-new worker (no stamps) takes the same path. A worker that missed
nothing costs one read. Settings → Resync Traefik remains the manual global
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
up in a filename or constructed identity — project, application, and domain
names, domain tags, volume names, and setting keys — is `AlphaNumericField`:
lowercase alphanumerics starting with a letter, **rejected, never cleaned**,
on any write path. Project/application names are derived from the free-text
`label` via `AlphaNumericField.clean`, which itself raises when no valid name
can be derived (no letters, or digits before the first letter); domain names,
tags, and volume names are typed directly by the user and rejected as-is on
violation. A qualified name therefore contains exactly one dash, so it is
injective and no qn can dash-prefix another — every `{qn}-` glob and prefix
match below is exact, and the compose service key (= qualified name, same as
`container_name`) is a unique Docker DNS name on `sage_default`. Worker
hostnames are Tailscale's identity, not ours: the one remaining
`CleanCharField` (dashes allowed, still rejected-not-cleaned on violation).
`ValueError` is the house convention for invalid user input — a global
exception handler in `api.py` (plus the generic route handlers, whose broad
`except` would otherwise swallow it) maps it to a 400 carrying the message, so
routes call `clean`/`validate` bare.

**`reconcile_traefik_configs`** (minutely, `platform` scope) is the declarative
existence backstop for everything the change-triggers can miss (crash-dropped
syncs, workers offline during a sync, app rows deleted before cleanup, workers
joining after a sync). It lists every online worker's `traefik/dynamic/`, then
reads the application set (in that order — app creation is an unscoped route
write, so listing first guarantees every listed file's owner is visible in the
read) and reconciles both directions: files matching no live application's
`{qn}-` prefix are removed by exact filename; an application with domains
(`domains_synced=True`) missing files on any online worker gets a sync
requested; and any app left `domains_synced=False` gets its sync re-enqueued.
On top of existence, the scan compares each online worker's revision stamps
against the recomputed `routing_input_hash` — a mismatch means the worker
holds files rendered from stale inputs (a missed sync) and requests that app's
sync; stamps for deleted applications are removed like their files. Rendering
correctness stays with the per-app sync.

`domains_synced` is a progress/backstop marker, not a trigger by itself, and no
DB signal maintains it (signals only keep the counters). Two rare paths that
touch many apps (`refresh_traefik` on a domain change, worker sync) loop the
affected apps and call the helper rather than bulk-setting the flag.

## Settings And Traefik Refresh

Cloudflare and Tailscale are configured by required env vars (`DOMAIN`,
`CLOUDFLARE_API_TOKEN`), so changing them is a
restart. On start, `setup_domain` sets up the new domain (zone, tunnel, the manager's
record, the manager's certificate) before serving, the `renew_certificates` startup run
issues the workers' certificate for a new domain, and the `check_stamps` worker sync
converges online workers: the `infra` stamp includes the domain and the tunnel id (a new
account means a new tunnel), the `certs` stamp follows the workers' certificate, and each
app's routing stamp includes the domain. Resources of a previous domain or account stay
in Cloudflare.

The `Setting` table holds S3 and notifications only; every field is optional and set from
the UI. A database, or a restored backup, can hold rows for keys that aren't defined; they
are dropped whenever the settings load (`Settings.load_all`), and `routes/settings.py` only
serves defined keys. UI updates follow one pattern:
validate the merged value, persist via `Settings().set(...)`, reload the consuming service
(`S3().load()`, `Notifications().load_notifications_config()`), then queue a check.

`refresh_traefik` (Resync Traefik) re-ensures both certificates, re-pushes the workers'
certificate and Traefik config, the `*.int` records and every app's routing.

## Templates

`app/templates/` contains generated runtime inputs for:

- worker compose files
- worker Traefik config
- worker Vector config
- worker application backup and restore scripts
