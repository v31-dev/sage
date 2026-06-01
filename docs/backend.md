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

Nested routes cover:

- applications
- containers
- domains
- volumes
- volume backups

The vector ingestion API is separate in `app/api_vector.py` and currently handles log ingestion at `/api/vector/logs`.

## Orchestration Layer

`app/services/manager.py` is the main backend coordinator. It handles:

- worker discovery and setup
- worker removal and online/offline transitions
- remote config sync over Tailscale
- deployment and stop flows
- Traefik/domain synchronization
- backup and restore operations
- notifications
- cleanup logic

Most high-level backend behavior ultimately flows through this service.

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
  - notification dispatch

## Logging And Task Context

The backend relies on `app/utils/logging.py` for request and task tracing.

Key rules:

- Every meaningful log line should carry a `task_id`.
- `task_id` lives in a `ContextVar`.
- FastAPI middleware creates a task id per request and returns it in `X-Task-ID`.
- Rocketry tasks are wrapped so scheduled and API-triggered tasks also run with a task id.

This is a project-wide pattern, not a local implementation detail.

## Async And Blocking Work

The project distinguishes between local database work and remote or network-bound work:

- Local Peewee + SQLite calls run inline.
- Blocking network or remote operations should go through `run_in_executor_with_context(...)`.
- The helper preserves `ContextVar` state, including `task_id`.

The codebase explicitly avoids `asyncio.to_thread()` for this reason.

## Deferred-Trigger Pattern (Pending-Flag On A Recurring Task)

When a user action (or an internal flow) needs to invoke an expensive operation that is already running on a recurring schedule, prefer signalling the *existing* scheduled task to run "in force mode" on its next tick — instead of spawning a parallel on-demand task that races with it.

This pattern keeps a single executor for the operation, eliminates concurrency hazards entirely, and works with both UI route handlers (async, FastAPI) and inline awaited flows (e.g. restore) without introducing locks.

### Mechanics

- Store a `threading.Event` on the relevant singleton service (e.g. `Manager.force_resync_pending`).
- Initialize it in the service's `__init__`.
- The recurring Rocketry task reads the flag, clears it if set, and passes the resulting boolean into the underlying operation:

  ```python
  @app.task(every("30 seconds"))
  async def manager_sync_workers():
    manager = Manager()
    force = manager.force_resync_pending.is_set()
    if force:
      manager.force_resync_pending.clear()
    await run_in_executor_with_context(manager.sync_workers, force=force)
  ```

- Callers (UI route handler, internal flow) request a force run by calling `.set()`:

  ```python
  manager.force_resync_pending.set()
  ```

- Re-clicks while a force is queued are no-ops; routes can detect this with `.is_set()` and return `409` for clarity.

### Why `threading.Event`

`threading.Event` is a stdlib primitive (since Python 2.6) — a thread-safe boolean flag with four operations:

| Method        | Effect                                                                  |
| ------------- | ----------------------------------------------------------------------- |
| `set()`       | Set the flag to True. Atomic.                                           |
| `clear()`     | Set the flag to False. Atomic.                                          |
| `is_set()`    | Non-blocking read of the current value. Atomic.                         |
| `wait(t)`     | Block the calling thread until the flag is True, or timeout. Unused here. |

Internally it wraps a `Condition` + `Lock`, so all four are safe across thread boundaries. There is no need for additional locking around set/clear/is_set sequences.

It is the right primitive for sage because:

- The setter runs in a FastAPI async route or an inline awaited flow (main event loop thread).
- The consumer runs in an executor worker thread (via `run_in_executor_with_context`).
- These are different threads, so a thread-aware primitive is required.
- `asyncio.Event` is single-thread-loop-bound and would not be safe here.
- A bare `bool` works in CPython by accident of the GIL but loses semantic clarity and the future option of `.wait(...)`.

### When to use it

- A user-facing button that triggers the same operation a scheduler already runs.
- A code path that needs to defer a force-mode run until after some other work has completed (e.g. signalling from inside a DB restore).
- Any case where you want *exactly one* executor for an operation but multiple callers that can request it.

### When not to use it

- The operation has no recurring task to attach to — write a dedicated Rocketry task instead.
- The caller needs synchronous confirmation that the work completed (the pattern is fire-and-forget; latency is bounded by the schedule interval).
- Multiple distinct *kinds* of triggers need to be distinguished. `Event` is a single boolean; richer state needs a different shape (e.g. a `queue.Queue` of trigger reasons).

### Used today

- `Manager.force_resync_pending` — signals the next `manager_sync_workers` tick to run with `force=True`. Set by the `/api/settings/resync_workers` route (UI button) and by `restore_database_from_s3` after a successful platform restore.

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

The scheduler in `app/scheduler.py` uses async Rocketry tasks.

Recurring tasks currently cover:

- worker sync
- application status sync
- daily system digest
- volume backup scheduling
- Traefik domain config sync
- metrics collection
- platform backup
- cleanup
- certificate sync
- Traefik refresh after Cloudflare setting changes

On-demand tasks currently cover:

- deploy application
- stop application
- delete container
- backup application
- delete backup from S3

## Settings And Traefik Refresh

Platform settings (`s3`, `notifications`, `cloudflare`) live in the `Setting` table. Env vars only seed empty fields on first boot.

UI updates in `routes/settings.py` follow the same pattern: validate the merged value, persist via `Settings().set(...)`, then reload the consuming service (`S3().load()`, `Notifications().load_notifications_config()`, `Cloudflare().load()`).

Cloudflare changes additionally schedule the `refresh_traefik` Rocketry task (in `services/manager.py`):

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
