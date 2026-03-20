# preface

**Purpose**: Lightweight project context for AI agents. NOT a requirements doc or roadmap — describes how the codebase currently works and the constraints to respect.

**Editing this file** (for the user): Keep it concise. Once a problem/solution is implemented, replace the block with a one-line factual description and point to the source file. Remove code examples once the code is in the repo. Target <150 lines total.

## For the User
Start new chats with: `Read APP.md. And I want to [your task here]...`

## For the AI Agent

**⚠️ DISCUSS BEFORE IMPLEMENTING — default mode is discussion, not code.**

- Question or problem description → diagnose and discuss first, do NOT write code
- Explicit instruction ("implement", "fix", "add", "change") → now you may write code

**Before starting:**
1. Read this entire document
2. Read Rocketry docs before touching scheduler or logging — do not guess at behaviour:
   - Execution model & multilaunch: https://rocketry.readthedocs.io/en/stable/handbooks/task/execution.html
   - Logging: https://rocketry.readthedocs.io/en/stable/handbooks/logging.html
   - Config: https://rocketry.readthedocs.io/en/stable/handbooks/config.html
3. Preserve existing patterns: task_id flow, service singletons, thread-safety, WAL mode

**Don't:** write code unprompted · ignore async/thread boundaries · change Rocketry without reading docs · break WAL mode · suggest architectural changes without understanding constraints

**Do:** discuss first · explain reasoning · validate task_id flow · test against documented constraints


# goals

Single-user micro-PaaS on a Tailscale network. 1 manager + N workers. Simplicity-first; scale by adding workers or upgrading manager.

- **Networking**: Tailscale links all nodes across any region/cloud. Public traffic via Cloudflare tunnels (`*.domain`), internal via Tailscale (`*.int.domain` workers, `*.core.domain` manager). No Docker Swarm (fails multi-region).
- **Worker stack**: Each worker runs cloudflared + Traefik + Vector + Glances — see `app/templates/worker/docker-compose.yml`.
- **Mesh resilience**: No manager ingress — workers serve traffic directly. Manager or worker failure is isolated.
- **Manager jobs**: Minutely sync of Cloudflare DNS + dynamic Traefik config to maintain mesh routing.
- **Manager HA**: Not required. Single node with S3 backup/restore for resilience.
- **Primary features**: Deploy apps from public GitHub repos (Dockerfile-based); manage lifecycle (start/stop/restart/destroy/scale/migrate); view container and system logs; view worker and system metrics; S3 backup/restore.
- **Security**: Out of scope — access is gated by Tailscale. UI at `sage.core.domain`.


# components

## app

### Architecture

3 concurrent servers in one process (`main.py`):
- **Main API** (port 9000): FastAPI, routes in `routes/`
- **Metrics API** (port 9001): Separate FastAPI for workers to push Vector logs/metrics (Tailscale ACL boundary)
- **Rocketry Scheduler**: Async cron-like scheduler (`scheduler.py`)

On startup, `Manager()` singleton initializes all service singletons. Any init failure → process exit.

### Services (`services/`)

Singleton pattern (`services/base.py`), initialized at startup, accessible everywhere. Each has a reentrant lock.
Services: `Database` (SQLite + WAL), `Tailscale`, `Cloudflare`, `Traefik`, `Metrics`.

### Scheduler (`scheduler.py`)

`Rocketry(execution="async")` — single-execution model (new trigger ignored if previous still running). All tasks are `async def`. Blocking I/O dispatched via `run_in_executor_with_context`.

Current tasks:
- `manager_sync_workers`: minutely — worker/database sync
- `collect_metrics`: minutely — concurrent worker HTTP fetches via `asyncio.gather` + semaphore(5)
- `traefik_sync_certs`: every 20 days
- `metrics_cleanup`: daily, prunes records >7 days old

### Hardware Constraints

Manager target: **well under 2c4g**. Every resource counts.
- No thread pools. Executor only for necessary blocking I/O (Peewee, SSH).
- Event loop (uvicorn) handles all I/O. Scheduler is async-first.
- Explicit, lightweight patterns only — no clever infrastructure.

**Scaling**: horizontal (add workers), vertical (upgrade manager only if truly bottlenecked).

### Database Sharding

- **Metrics**: one SQLite file per hostname — `metrics/<hostname>.db`. No cross-node contention.
- **Logs**: one SQLite file per container — `logs/<container>.db`. No cross-container contention.
- WAL mode allows concurrent reads alongside a writer on each shard.

### Container Logs

Vector on each worker sends Docker logs to manager port 9001 (`POST /logs`). Stored in SQLite per container with FTS5 full-text search. Query via `GET /workers/{hostname}/logs/{container}?search=...&from_ts=...&to_ts=...&since_ts=...` (see `services/metrics.py`, `routes/vector.py`, `routes/workers.py`). Frontend at `ui/src/pages/Logs.vue` with polling, search, date filtering, and incremental fetching.

### task_id & Logging

**Rule**: every log record must carry a task_id (8-char UUID prefix). No blank task_ids on meaningful work.

**Infrastructure** (see `utils/logging.py`, `scheduler.py`):
- `ContextVar` (`task_id`) is the single source of truth. `ContextVarFilter` stamps it on every log record.
- **FastAPI middleware**: generates token, sets ContextVar + `request.state.task_id`, returns `X-Task-ID` header.
- **Rocketry tasks**: `LoggedRocketry` subclass wraps every task with `with_task_id` — pops `_task_id` kwarg (if API-triggered) or generates fresh UUID (if scheduled); emits `started`/`completed`/`failed` boundary logs automatically.
- **API → task trigger**: `LoggedSession._Proxy.run()` injects `_task_id=task_id.get()` before calling Rocketry's `.run()`, bridging FastAPI context into the task.
- **Multilaunch**: each concurrent run is a separate asyncio Task with its own ContextVar — works without changes.
- Rocketry's own `rocketry.task` logger lines appear with blank task_id (fire outside `with_task_id`) — acceptable.

**Flow rules**:

| Scenario | Pattern |
|----------|---------|
| Async route / task → service calls | Implicit — ContextVar propagates automatically |
| Sync `def` route → service calls | Implicit — anyio copies context to thread automatically |
| Any async → blocking I/O | Explicit — `run_in_executor_with_context(func, *args)` |
| API → Rocketry trigger | `LoggedSession._Proxy` injects `_task_id` kwarg |

**`run_in_executor_with_context`**: captures `copy_context()` in async context (carries current `task_id`), submits `ctx.run(func, *args)` to the default thread pool. Use this for all blocking Peewee/SSH/HTTP calls.

**Pitfalls**:
- Never use `asyncio.to_thread()` — context not preserved. Always use `run_in_executor_with_context`.
- Never pass `task_id` as a function parameter (except at the scheduler trigger boundary). Let ContextVar flow.

## ui

Vue 3 — see `ui/README.md`. Lower priority; focus on Python app performance and logic.