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
- worker config refresh after settings changes

On-demand tasks currently cover:

- deploy application
- stop application
- delete container
- backup application
- delete backup from S3

## Templates

`app/templates/` contains generated runtime inputs for:

- manager Traefik config
- manager Vector config
- worker compose files
- worker Traefik config
- worker Vector config
- worker application backup and restore scripts
