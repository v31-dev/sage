# AGENTS — app (manager backend)

Non-negotiable patterns for the manager backend. Read the root [../AGENTS.md](../AGENTS.md) first for the architecture and repo-wide rules; this file is backend-specific.

## Manager Networking (userspace Tailscale)

- Inbound tailnet traffic to the manager is delivered to `127.0.0.1:<port>` inside the container, so anything listening there (on `0.0.0.0` or `127.0.0.1`) is reachable from the tailnet wherever the tailnet policy allows. Never put an internal-only listener on `127.0.0.1`; use `127.0.0.2` or a unix socket.
- The container has no route into the tailnet: SSH to workers goes through `tailscale nc` (asyncssh `proxy_command`), for HTTP the workers reach the manager (Telegraf's reports and Vector's logs on 9001), not the other way round. Internet calls (Cloudflare, S3, ACME, GitHub) go direct.
- `sync_file` creates every file with its mode under a temporary name and renames it into place, so a reader never finds a partial file. A file with a secret in it goes with `mode=0o600` (a script `0o700`; the default is `0o644`): Tailscale SSH ignores the mode of a new directory, so the file's own mode is what keeps it private.
- Optional services (S3 `s3_config`, notifications) are checked before use: scheduled triggers skip quietly when unconfigured, user actions get a clear 4xx. Cloudflare is always configured; don't add checks for it.

## Services

- Services use the singleton base in `app/services/base.py`.
- Service mutations should remain thread-safe.
- Do not introduce alternate service lifecycle patterns without a clear reason.
- In-memory state derived from the DB or settings (setting values, the S3 client, notification config, the worker list) is refreshed wherever that data is replaced: a settings save, a restore.
- `GET /api` is the `info` topic of the UI's Socket.IO connection, recomputed whenever one of its sources fires (a source fires only when what its owner holds differs, so a state that `info` shows needs a source), and the pause banner and the restore overlay rely on it, so it reads memory only: no network, no database.
- A Socket.IO topic is for a change the page did not make, so name that change before proposing one; a page's own action is shown by the page. The REST API stays complete: a route keeps every field whether or not a topic carries it. The rule with examples is in Pushed Updates in [docs/architecture.md](../docs/architecture.md).

## Logging And `task_id`

- `task_id` flows through `ContextVar` in `app/utils/logging.py`.
- The queue's `run_task` sets it for each queued task; a request has none.
- The offload helpers copy the context, so worker threads keep the originating `task_id`.
- Do not pass `task_id` through regular function parameters.

## Async Boundaries

- Offload blocking network/remote I/O — never `asyncio.to_thread()` (it runs on the default executor, outside the lane pools). Both helpers live in `app/utils/executor.py`:
  - `run_in_executor_with_context(...)` — awaited, from async code; offloads to the task's lane pool (ambient `active_executor`) or an explicit one, and raises if neither is set.
  - `submit_with_context(...)` — fire-and-forget, no running loop required (e.g. notifications).
- A queued task's lane is declared once at `add_task`; offloads inside it inherit it and don't re-name a pool.
- Fan-out orchestrators (`asyncio.gather` over offloads) must be async; never block a sync task on its own lane's pool — it deadlocks a single-worker lane.
- Local Peewee and SQLite calls run inline: SQLite on local disk is not an external call. Heavy file I/O is different (bulk writes in a loop, backup and restore copies, encryption, large listings): offload it, and write in one transaction (`db.atomic()`, `insert_many`) so the loop stays responsive.

## Database

- Main state is stored in SQLite with WAL mode. The database is deferred: `Database` opens it at `config.DB_PATH` at startup, and importing the models touches no disk.
- Metrics are stored in per-host SQLite files.
- Times are naive UTC, the way the UI reads a timestamp that has no zone. Read the clock through `utils.common.utcnow()`; ruff (`DTZ003`, `DTZ005`) rejects `datetime.utcnow()` and a `datetime.now()` without a zone. Retry with backoff goes through `utils.retry` (`retry`, `retry_async`, and `is_transient` as the one meaning of "transient").
- Logs are stored in per-application SQLite files with FTS5 search; Traefik's access lines as requests in columns (`logs/requests.db`); a queued task's lines in `logs/tasks.db`, read by task id. The manager's other lines are in stdout only (`docker logs`).
- Preserve the existing lightweight storage approach unless the task explicitly requires otherwise.
- The live database (`data-gitignore/data.db`) is only ever read: open it with `sqlite3.connect('file:...?mode=ro', uri=True)`, and never run INSERT, UPDATE, DELETE or DDL against it. To exercise a write path, copy it to the session's scratch space and point `config.DB_PATH` at the copy.

## Migrations And One-Off Changes

- Do not add one-time migration/backfill/convergence code (DB rows, files, or external managed state like Cloudflare DNS records) just to bring existing deployments in line with a change. Prefer documenting the manual step in the summary/PR over shipping code that runs once and is then dead weight.
- The one sanctioned automatic migration is DB schema evolution — missing columns are added at boot and columns no model has are dropped (`Database` in `app/services/db/database.py`). Anything beyond that (data backfills, external-state rewrites) is a manual step unless the user says otherwise.
- New writes should carry the new desired state going forward (e.g. a new default on create); reconciling what already exists is a documented manual action, not code.

## Operation Queue And Scheduler

- APScheduler is a pure cron/interval trigger: each scheduled coroutine only calls `Manager().add_task(...)`. All execution and concurrency control live in the in-memory `TaskQueue` (`app/utils/queue.py`).
- Route and scheduled work alike enqueue via `add_task`; handlers take ids (not ORM objects) and re-fetch. A target row deleted before the task runs is a legitimate interleaving: re-fetch with `get_or_none` and treat a missing row as a quiet no-op (log + return), never a crash.
- Scopes (`platform`, `app`/`app:<qualified_name>`, `common`, `metrics`) give hierarchical mutual exclusion (enforced by the dispatcher); each scope root has its own lane pool. `common` is a single-thread, low-priority lane (settings checks, log cleanup, release refresh): its tasks may wait, so nothing urgent goes there. Admission (drop vs. enqueue) is one `on_conflict` enum on `add_task`: `DEDUP` (default, skip if an identical op — same name + exact scope — is already pending/running, else enqueue and wait), `QUEUE` (always enqueue, no dedupe), `REPLACE` (latest-wins, supersede a pending duplicate then wait). A different op holding a conflicting scope never drops a new task — it defers behind it. `quiet=True` is a separate flag for scheduled background housekeeping: no completed or cancelled row (the Task table is the history of operations worth debugging) and it never makes a settings section "applying". Details in [docs/backend.md](../docs/backend.md).
- Task shape: one async task that `asyncio.gather`s its offloaded leaves (independent work in one operation, shared scope), per-entity fan-out (one `add_task` per entity, each with its own scope/lifecycle/DEDUP), or sequential `await`s (ordered/dependent steps). A fan-out parent must be async — never a sync task blocking on its own lane.
- When adding or changing a task, declare its `scopes`, `executor`, and `on_conflict` at the single `add_task` call site and **add/update its row in the Task Catalog** in [docs/backend.md](../docs/backend.md). Worker convergence (`sync_workers`/`setup_worker`) holds `{platform}` only — app tasks treat workers as unreliable and fast-fail on offline-flagged ones, so worker churn never barriers app work; `remove_worker`, `refresh_traefik`, and the cert sync keep the broad `{platform, app}` barrier. There is deliberately no per-worker scope dimension — don't add one.
- Scheduler cadence lives in `app/scheduler.py` (`AsyncIOScheduler`): sub-minute pumps use `IntervalTrigger`, everything else uses `CronTrigger.from_crontab`. Gotcha: APScheduler's `from_crontab` treats numeric day-of-week `0` as **Monday**, while the `croniter`-matched per-app volume-backup crons use standard `0` = Sunday — be explicit about the intended weekday. `dispatch_tick` must stay `async` (coroutine jobs run on the loop; a sync job runs in a worker thread where its `asyncio.create_task` has no running loop).
- Sage never restarts or upgrades itself. Pause, restore and stop are a `TaskQueue` state (`Manager().pause_tasks()` etc.): paused, nothing starts but cron work still queues (DEDUP/REPLACE keep one copy each); restoring is paused except for the platform restore itself, and the UI is blocked; a stop signal drains (queued and later-added tasks cancelled, running ones finish). Routes use the Manager, not `scheduler` — only `main.py` imports it. While not running, `app/api.py` refuses changes with 503 (every method but GET/HEAD/OPTIONS, except pause/resume), so a route that only reads is a GET; pause comes only before a shutdown or an upgrade, so nothing else has to work while paused. See "Pause And Stop" in [docs/backend.md](../docs/backend.md).
- Verify task flow, logging flow, and async boundaries when touching orchestration code, including overlapping calls and a cache replaced while another thread reads it.

## Testing

- Tests for the core engine (the task queue, the reconcilers and the status decisions) are exhaustive and may be slow: real scenarios on the real class (deploys and syncs crossing on shared scopes, restores, pauses, shutdowns) with mock task bodies, seeded random delays and failures, producers on several threads, and an independent model that predicts each admission and dispatch decision. Check the invariants and the model on every step, and plant bugs to see the tests fail. Correct admission is the main criterion, and scope exclusion the safety property. Mocks stand in for task bodies only, never for the decisions under test.
- A change that depends on how Sage is reached is tested through that shape: uvicorn serving TLS itself, as in production, and the Traefik image from `docker-compose.yml` terminating TLS in front of the real app (for a local run: on the host network, with a self-signed certificate and a router like the one in `dev/traefik/dynamic.yml`). Connect the way a browser does, send the proxy's headers per request type, and say what was and wasn't exercised. A "not verified" line in the docs is unfinished work.
- Check a change to `app/templates/worker/vector/vector.yml` with the workers' Vector image instead of guessing at VRL; Vector Config in [docs/development.md](../docs/development.md) has the commands.

## Don't

- Don't break `task_id` propagation.
- Don't replace the SQLite/WAL model with heavier infrastructure without a clear requirement.
