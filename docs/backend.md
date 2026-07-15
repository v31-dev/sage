# Backend

## Stack

- Python 3.12
- FastAPI
- APScheduler + croniter
- Peewee + SQLite
- httpx
- Cloudflare SDK
- Tailscale CLI
- boto3 + aioboto3

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

The vector ingestion API is separate in `app/api_vector.py` and currently handles log ingestion at `/api/vector/logs`.

## Orchestration Layer

`app/services/manager/` is a package of mixins composed into the `Manager` singleton, the main backend coordinator. It handles:

- worker discovery and setup
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
  - persistent settings state plus env fallback
- `services/cloudflare.py`
  - Cloudflare DNS and tunnel management
- `services/tailscale.py`
  - Tailscale status, SSH command execution, file sync
- `services/traefik.py`
  - manager-side and worker-side Traefik config generation and certificate sync
- `services/metrics.py`
  - metrics collection plus per-container log storage and search
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
- **Dispatch.** A one-second APScheduler interval tick calls `dispatch_tick`, which starts every pending task whose scope is free. One dispatcher makes the scan race-free; a `threading.Lock` guards the queue because producers run on FastAPI/loop threads.
- **Admission policy — `on_conflict` on `add_task`** (enum `OnConflict`; decided atomically under the queue lock). Admission decides *drop vs. enqueue*; mutual exclusion is **separate and always scope-based** — the dispatcher serializes conflicting scopes regardless of mode, so an enqueued task simply waits for its scope to free.
  - `DEDUP` (default) — drop and return `False` if an identical op (**same name + exact scope**) is already running or queued; otherwise enqueue and wait. Prevents reconciler pile-ups and double-triggers (e.g. a second deploy of the same app). A *different* op holding a conflicting scope does **not** cause a drop — it enqueues and waits behind that op. (So a background sync never rejects a user action; the action just defers behind it.)
  - `QUEUE` — always enqueue and wait, even for an identical op. For work where every call must run, including params-identified work that shares a name + scope (S3 delete by path, worker removal by host, the settings Traefik refresh by change-flags).
  - `REPLACE` — cancel any **pending** duplicate (same name + exact scope; latest-wins), then enqueue and wait. Used by the platform backup (cron + UI enqueue identically: at most one backup queued behind a running one) and the worker force-resync.
  - `DEDUP`/`REPLACE` identity is **name + scope, not params**. DEDUP rejecting a params-distinct call is safe (it returns `False`/409, nothing is lost); REPLACE would *silently* drop it, so params-identified work uses `QUEUE`.
- **`priority`** (separate flag) — insert ahead of lower/equal work but behind dominating scopes.
- **`quiet`** (separate flag) — record only on failure (see below).
- **Persistence.** The queue calls a `record` hook (`Manager._persist_task`) at each terminal state. Only `completed`/`failed`/`cancelled` are written to the `Task` table; running and queued tasks live in memory and are exposed via `snapshot()` for the UI. `quiet=True` tasks skip the `completed` record so high-frequency reconcilers (metrics collection, per-app status/domain sync, the 30s worker sync) don't flood the table — failures still record. Status vocabulary: `cancelled` means the task **never started** (REPLACE supersession, the pre-restart drain); a task interrupted **mid-run** — the loop's shutdown cancels its coroutine — records as `failed` (started but did not complete).

To make a deliberate user action coalesce with a recurring one, enqueue both with `on_conflict=OnConflict.REPLACE` (latest-wins; e.g. the platform backup and the worker force-resync); no per-operation locks or pending flags are needed.

## Execution Modes And Pools

Pools (lanes) are defined in `app/utils/executor.py`. The first four are queue lanes wired into the `TaskQueue`; the rest are off-queue.

| Pool | Workers | Used by |
| --- | --- | --- |
| `PLATFORM_EXECUTOR` | 1 | `platform` lane — main-DB backup/restore, cleanup, restart |
| `COMMON_EXECUTOR` | 1 | `common` lane — version refresh, daily summary, S3 delete |
| `APP_EXECUTOR` | 6 | `app` lane — per-application deploy/stop/backup/restore |
| `METRICS_EXECUTOR` | 2 | `metrics` lane — collection, metrics-store cleanup |
| `NOTIFICATIONS_EXECUTOR` | 1 | `Manager.notify` webhook sends (off-queue, fire-and-forget) |
| `LOGS_EXECUTOR` | 1 | Vector log ingestion (off-queue, request-path; single SQLite writer) |

Worker counts are **fixed I/O-concurrency limits, not derived from `os.cpu_count()`** — these lanes run blocking network I/O (Tailscale, S3, httpx), so the manager's core count doesn't gate them, and in a CPU-limited container `os.cpu_count()` reports host cores rather than the container's allowance. `platform`/`common` have no child scopes (their tasks never run concurrently) so they stay at 1; `app`/`metrics` are sized for the realistic ceiling (≤10 workers, ≤50 apps, a couple of parallel deploys). Concurrent DB writes from `app`-lane threads are safe: Peewee keeps per-thread SQLite connections and the DB runs WAL + `busy_timeout`.

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
- log SQLite shards with FTS5
- Traefik and Vector runtime config written to mounted directories

## Scheduler Notes

`app/scheduler.py` holds async APScheduler triggers that act only as cron/interval triggers — each one calls `Manager().add_task(...)` and contains no execution logic. Sub-minute pumps (`dispatch_tick`, `sync_workers`) use `IntervalTrigger`; everything else uses `CronTrigger.from_crontab` and fires on exact wall-clock instants (never at boot). Note: APScheduler's `from_crontab` treats numeric day-of-week `0` as Monday (whereas the `croniter`-matched volume-backup crons use `0` = Sunday) — be explicit about the intended weekday. `dispatch_tick` must stay an `async def` (APScheduler runs coroutine jobs on the loop; a sync job would run in a worker thread where its `asyncio.create_task` has no running loop). Route handlers enqueue through the same `add_task` path, and a few tasks are enqueued internally from within another task.

## Task Catalog

Every queued operation, its scope, where it is enqueued from, its lane pool, and its `on_conflict` policy. Keep this current when adding, removing, or re-scoping a task. `<qn>` is an application's `qualified_name`; `<host>` a worker hostname.

**Scheduled (APScheduler cron/interval → `add_task`)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `sync_workers` | platform, app | interval 30s | platform | DEDUP |
| `sync_application_status` | app:`<qn>` | cron 1m | app | DEDUP |
| `backup_application_s3` | app:`<qn>` | cron 1m (due backups) | app | DEDUP |
| `reconcile_traefik_configs` | platform | cron 1m | platform | DEDUP |
| `Metrics.collect` | metrics:`<host>` | cron 1m | metrics | DEDUP |
| `send_summary_notification` | common | cron daily 08:00 | common | REPLACE |
| `get_latest_version` | common | cron 4h | common | REPLACE |
| `backup_database_s3` | platform, app | cron 6h | platform | REPLACE |
| `Metrics.cleanup` | metrics | cron daily 04:00 | metrics | REPLACE |
| `cleanup` | platform | cron daily 04:00 | platform | REPLACE |
| `sync_traefik_certificates` | app | cron weekly (Mon 03:00) | app | REPLACE |

**Route-driven (HTTP → `add_task`)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `deploy_application` | app:`<qn>` | POST …/deploy | app | DEDUP |
| `stop_application` | app:`<qn>` | POST …/stop | app | DEDUP |
| `delete_container` | app:`<qn>` | DELETE …/containers/{c} | app | QUEUE (route rejects a duplicate delete of the same container via `has_task`, the params-aware pending/running check) |
| `backup_application_s3` | app:`<qn>` | POST …/volumes/{v}/backups | app | DEDUP |
| `restore_application_volume_from_s3` | app:`<qn>` | POST …/backups/{b}/restore | app | DEDUP |
| `backup_database_s3` | platform, app | POST /backups | platform | REPLACE |
| `restore_database_from_s3` | platform, app | POST /backups/{b}/restore | platform | DEDUP |
| `delete_backup_s3` | common | DELETE /backups/{b} | common | QUEUE |
| `remove_worker` | platform, app | DELETE /workers/{h} | platform | QUEUE |
| `refresh_traefik` | platform, app | PUT /settings/cloudflare | platform | QUEUE |
| `refresh_traefik` | platform, app | POST /settings/resync_traefik | platform | DEDUP |
| `restart` | platform, app, common, metrics | POST /settings/restart | platform | QUEUE (+priority) |
| `sync_workers` | platform, app | POST /settings/resync_workers | platform | REPLACE |
| `sync_application_traefik_domains_config` | app:`<qn>` | create/update/delete_domain, update-container tag change (via `request_application_traefik_sync`) | app | REPLACE |

Per-app tasks (status sync, the Traefik domains sync, backups, `delete_container`) re-fetch their target by id with `get_or_none` and treat a missing row as a **quiet no-op**: instant deletes are a legitimate interleaving with queued/scheduled work, so a task whose target vanished has nothing to do (the reconcile scan owns worker-side cleanup for deleted apps). `remove_worker` is the deliberate exception — its precondition re-check fails loudly because proceeding would destroy a worker that just gained containers.

Container create and tag-update are **instant route-side model writes**, like every other entity's CRUD — not queued tasks. The `container_count` signal recomputes atomically from the rows and `only_save_dirty` keeps concurrent status saves from clobbering it, so no serialization is needed; only `delete_container` stays queued (it does remote cleanup). A queued deploy reads whatever is committed when it starts — see the snapshot contract (todo item 2).

**Internal (enqueued from within another task)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `sync_workers` | platform, app | `restore_database_from_s3` | platform | REPLACE |
| `sync_application_traefik_domains_config` | app:`<qn>` | deploy/stop, delete-container, `sync_application_status`, `sync_workers`, `refresh_traefik`, `reconcile_traefik_configs` missing-file check (via `request_application_traefik_sync`) | app | REPLACE |
| `sync_application_traefik_domains_config` | app:`<qn>` | `reconcile_traefik_configs` `domains_synced=False` backstop | app | DEDUP |

Quiet tasks (recorded only on failure): `sync_workers` (cron), `sync_application_status`, `sync_application_traefik_domains_config`, `reconcile_traefik_configs`, `Metrics.collect`, `Metrics.cleanup`.

Cron cadence convention: **≤ 1m → `DEDUP`** (a high-frequency reconciler skips only if its own previous run is still in flight; the next tick retries), **> 1m → `REPLACE`** (a 6h/1d/10d task must not skip its whole cycle on a transient conflict; latest-wins keeps no backlog).

`sync_workers` deliberately differs by source — cron uses `DEDUP`, while resync/restore use `REPLACE` with `{force: True}` to supersede a pending plain sync. That is why DEDUP/REPLACE identity is name + scope and not params. The full-stack `restart` cancels pending work and waits with priority for in-flight platform/app operations before replacing the process.

**Interrupted-operation recovery.** A busy status (`deploying`/`stopping`/`backup`/`restoring`) is only ever written by a task while it holds that app's scope — and `sync_application_status` holds that same scope while it runs, so by mutual exclusion any busy status it observes has no owning operation left: the mark of an interrupted task (crash, plain container restart, cancelled coroutine). Instead of skipping such apps, the sync resets the stuck app/containers to `error` (with a notification) and then converges them against real `docker ps` state in the same run. This is why no boot-time status reset exists: the minutely sync covers startup wedges and mid-flight loss with one mechanism. Detection defers while a broad `app`-scope task runs (the per-app syncs queue behind it) and resumes when it finishes. Deploy/stop container children additionally run their whole body inside their try block so an early failure can never kill the parent's gather and leave detached siblings running.

Worker-infrastructure tasks (`setup_worker`/`sync_workers`, the worker cert sync, `refresh_traefik`) hold the broad `app` scope rather than a per-worker one **deliberately**: there is no `worker` scope dimension, and the breadth is what closes the *new-worker race* — a worker's row exists (with `online=False`) from the start of `setup_worker`, and container-create doesn't require `online`, so a deploy could target a worker that is still mid-reconfiguration. Blocking all app work for the duration is correct rather than landing a deploy on a half-set-up worker. The cost (a platform-wide app-op pause) is acceptable because these tasks are rare (worker churn; 10-day cert rotation), not steady-state. This includes the cert sync's wait-for-provisioning loop (up to 10 min holding the scope): it only spins while the manager's acme.json is invalid, a window in which routing is broken platform-wide anyway, and the sync must restart each worker's traefik to load new certs — deferring would fragment the disruption, not avoid it. A narrower per-worker scope would either miss the new-worker race or require every container-touching op to declare its workers' scopes (fragile); not worth it at the target scale (≤10 workers).

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
container, `sync_application_status` health flips, and Cloudflare domain change.
It is deliberately **not** called when routing is unaffected: adding a container
(inactive until deployed), or editing/deleting an inactive one.

**Worker join/rejoin runs full setup.** Any worker whose state may be stale —
brand new, back from an outage, or left half-configured by an interrupted setup
— goes through idempotent `setup_worker`: the offline→online transition in
`sync_workers` calls it directly (there is no lighter "mark online" path).
Setup re-syncs all infra files and ends with a
`request_application_traefik_sync` for every application **hosted on the
worker**. Apps hosted elsewhere are deliberately not re-synced — a rejoin must
never blip unrelated apps — so their routing files on the rejoined worker can
be *topology-stale* (routing content is a function of domains/tags/active
containers, not env or image, so only topology changes during the outage
matter) until that app's next routing change; Settings → Resync Traefik is the
deliberate global heal. The minutely `reconcile_traefik_configs` enforces
existence in between.

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
routes call `clean`/`validate` bare; the container routes validate the tag at
the route because the actual write happens later inside the queued task.

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
Content correctness stays with the per-app sync — the scan checks existence
only.

`domains_synced` is a progress/backstop marker, not a trigger by itself, and no
DB signal maintains it (signals only keep the counters). Two rare paths that
touch many apps (`refresh_traefik` on a domain change, worker sync) loop the
affected apps and call the helper rather than bulk-setting the flag.

## Settings And Traefik Refresh

Platform settings (`s3`, `notifications`, `cloudflare`) live in the `Setting` table. Env vars only seed empty fields on first boot.

UI updates in `routes/settings.py` follow the same pattern: validate the merged value, persist via `Settings().set(...)`, then reload the consuming service (`S3().load()`, `Notifications().load_notifications_config()`, `Cloudflare().load()`).

Cloudflare changes additionally enqueue the `refresh_traefik` operation (in `services/manager/traefik.py`):

- `admin_email` change — Manager Traefik static config rewritten; Manager Traefik restarted; worker Traefik configs resynced and restarted.
- `domain` change — additionally clears `acme.json` so Manager Traefik re-issues certs, then syncs the bundle to workers and updates worker DNS records.
- `api_token` change — token file at `/etc/traefik/cloudflare_dns_api_token` is rewritten; Manager Traefik restarted (workers untouched).

The token reaches Traefik via `CLOUDFLARE_DNS_API_TOKEN_FILE`, so rotation only needs a container restart instead of a compose recreate.

## Templates

`app/templates/` contains generated runtime inputs for:

- manager Traefik config
- manager Vector config
- worker compose files
- worker Traefik config
- worker Vector config
- worker application backup and restore scripts
