# Backend

## Stack

- Python 3.12
- FastAPI
- Rocketry
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

The Manager owns a single in-memory `TaskQueue` (`app/utils/queue.py`). It is the one place concurrency is controlled; Rocketry only enqueues.

- **Enqueue.** Every operation runs via `Manager().add_task(task=..., scopes=..., executor=..., params=..., ...)`. Handlers receive ids (not ORM objects) and re-fetch, so nothing carries a stale snapshot across the queue boundary.
- **Scopes** are `:`-delimited and hierarchical. Roots: `platform`, `app` (with `app:<qualified_name>` children), `common`, `metrics`. A parent scope conflicts with all of its children; siblings never conflict. Conflicting tasks are mutually excluded.
- **Dispatch.** A one-second Rocketry tick calls `dispatch_tick`, which starts every pending task whose scope is free. One dispatcher makes the scan race-free; a `threading.Lock` guards the queue because producers run on FastAPI/loop threads.
- **Admission flags on `add_task`:**
  - `queue` — default `False` rejects when the scope is busy (drops the task, returns `False`); `True` waits in line instead. Use `False` for retry-friendly reconcilers and rejectable user actions; `True` for deliberate actions that must not be dropped (a backup behind a deploy, an S3 delete).
  - `cancel_existing` — drop any pending task with the same name and a conflicting scope before adding (latest-wins). It matches on **name + scope, not params**, so only use it for idempotent, params-light work (worker resync, platform backup) — never where params identify the work (volume ids, an s3 path).
  - `priority` — insert ahead of lower/equal work but behind dominating scopes.
  - `quiet` — record only on failure (see below).
- **Persistence.** The queue calls a `record` hook (`Manager._persist_task`) at each terminal state. Only `completed`/`failed`/`cancelled` are written to the `Task` table; running and queued tasks live in memory and are exposed via `snapshot()` for the UI. `quiet=True` tasks skip the `completed` record so high-frequency reconcilers (metrics collection, per-app status/domain sync, the 30s worker sync) don't flood the table — failures still record.

To make a deliberate user action coalesce with a recurring one, enqueue it with `cancel_existing=True, queue=True` (e.g. the worker force-resync); no per-operation locks or pending flags are needed.

## Execution Modes And Pools

Pools (lanes) are defined in `app/utils/executor.py`. The first four are queue lanes wired into the `TaskQueue`; the rest are off-queue.

| Pool | Workers | Used by |
| --- | --- | --- |
| `PLATFORM_EXECUTOR` | 1 | `platform` lane — main-DB backup/restore, cleanup, restart |
| `COMMON_EXECUTOR` | 1 | `common` lane — version refresh, daily summary, S3 delete |
| `APP_EXECUTOR` | `2N-2` (min 1) | `app` lane — per-application deploy/stop/backup/restore |
| `METRICS_EXECUTOR` | 1–2 | `metrics` lane — collection, metrics-store cleanup |
| `NOTIFICATIONS_EXECUTOR` | 1 | `Manager.notify` webhook sends (off-queue, fire-and-forget) |
| `LOGS_EXECUTOR` | 1 | Vector log ingestion (off-queue, request-path; single SQLite writer) |

A task callable is sync or async, and `run_task` handles both:

- **async task** — awaited on the event loop; it fans blocking leaves out to its lane with `run_in_executor_with_context`. The task coroutine itself never occupies a pool worker.
- **sync task** — run entirely on its lane pool via `run_in_executor_with_context`.

**Ambient executor.** Before running a task, `run_task` binds its lane to the `active_executor` `ContextVar`. Any `run_in_executor_with_context(fn)` inside the task with no explicit `executor=` offloads to that lane, so the lane is declared once at `add_task` and inner call sites don't repeat it. `active_executor` is a `ContextVar`, so concurrent tasks on different lanes never see each other's binding.

**Fan-out is safe because orchestrators are async.** `deploy_application`, `stop_application`, and the backup/restore container loops `await asyncio.gather(...)` over offloaded children. The async parent runs on the loop (holds no worker), so children just queue and drain on the lane pool — no parent-starves-child deadlock. A task is marked "running" once its scope is acquired, independent of pool-thread availability, so its offloads may briefly queue inside the pool; that is bounded throughput, not a stall. The invariant: **never make a sync task that submits to its own lane and blocks on the result** — that deadlocks a single-worker lane. (`run_in_executor_with_context` enforces part of this by requiring a running loop; keep any raw `submit` calls fire-and-forget.)

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

`app/scheduler.py` holds async Rocketry tasks that act only as cron triggers — each one calls `Manager().add_task(...)` and contains no execution logic.

Recurring triggers currently cover:

- worker sync (`quiet`)
- application status sync (`quiet`)
- daily system digest
- volume backup scheduling
- Traefik domain config sync (`quiet`)
- metrics collection (`quiet`)
- platform backup
- cleanup
- certificate sync

Route-driven operations enqueue through the same `add_task` path: deploy, stop, delete container, application/volume backup, restore, worker removal, platform backup/restore, delete backup from S3, and the full-stack restart (which cancels pending work and waits with priority for in-flight platform/app operations before replacing the process).

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
