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
- **Persistence.** The queue calls a `record` hook (`Manager._persist_task`) at each terminal state. Only `completed`/`failed`/`cancelled` are written to the `Task` table; running and queued tasks live in memory and are exposed via `snapshot()` for the UI. `quiet=True` tasks skip the `completed` record so high-frequency reconcilers (metrics collection, per-app status/domain sync, the 30s worker sync) don't flood the table — failures still record.

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
| `sync_application_traefik_domains_config` | app:`<qn>` | cron 1m | app | DEDUP |
| `Metrics.collect` | metrics:`<host>` | cron 1m | metrics | DEDUP |
| `send_summary_notification` | common | cron daily 08:00 | common | REPLACE |
| `get_latest_version` | common | cron 6h (:15) | common | REPLACE |
| `backup_database_s3` | platform, app | cron 6h | platform | REPLACE |
| `Metrics.cleanup` | metrics | cron daily 04:00 | metrics | REPLACE |
| `cleanup` | platform | cron daily 04:00 | platform | REPLACE |
| `sync_traefik_certificates` | app | cron weekly (Mon 03:00) | app | REPLACE |

**Route-driven (HTTP → `add_task`)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `deploy_application` | app:`<qn>` | POST …/deploy | app | DEDUP |
| `stop_application` | app:`<qn>` | POST …/stop | app | DEDUP |
| `delete_container` | app:`<qn>` | DELETE …/containers/{c} | app | DEDUP |
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

**Internal (enqueued from within another task)**

| Task | Scope | Source | Lane | on_conflict |
| --- | --- | --- | --- | --- |
| `sync_workers` | platform, app | `restore_database_from_s3` | platform | REPLACE |

Quiet tasks (recorded only on failure): `sync_workers` (cron), `sync_application_status`, `sync_application_traefik_domains_config`, `Metrics.collect`, `Metrics.cleanup`.

Cron cadence convention: **≤ 1m → `DEDUP`** (a high-frequency reconciler skips only if its own previous run is still in flight; the next tick retries), **> 1m → `REPLACE`** (a 6h/1d/10d task must not skip its whole cycle on a transient conflict; latest-wins keeps no backlog).

`sync_workers` deliberately differs by source — cron uses `DEDUP`, while resync/restore use `REPLACE` with `{force: True}` to supersede a pending plain sync. That is why DEDUP/REPLACE identity is name + scope and not params. The full-stack `restart` cancels pending work and waits with priority for in-flight platform/app operations before replacing the process.

Worker-infrastructure tasks (`setup_worker`/`sync_workers`, the worker cert sync, `refresh_traefik`) hold the broad `app` scope rather than a per-worker one **deliberately**: there is no `worker` scope dimension, and the breadth is what closes the *new-worker race* — a worker's row exists (with `online=False`) from the start of `setup_worker`, and container-create doesn't require `online`, so a deploy could target a worker that is still mid-reconfiguration. Blocking all app work for the duration is correct rather than landing a deploy on a half-set-up worker. The cost (a platform-wide app-op pause) is acceptable because these tasks are rare (worker churn; 10-day cert rotation), not steady-state. A narrower per-worker scope would either miss the new-worker race or require every container-touching op to declare its workers' scopes (fragile); not worth it at the target scale (≤10 workers).

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
