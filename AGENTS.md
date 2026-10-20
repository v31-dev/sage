# AGENTS

This is a living project-context file for AI coding agents. Keep it concise, current, and focused on durable rules that should survive across sessions, handoffs, and context compaction.

## Start Here

- Read [README.md](README.md) first.
- Read the relevant files under [docs/](docs/) before making changes.
- Treat `README.md` plus the focused docs as the primary project context.

## Task Mode

- Default to discussion first when the user is describing a problem, asking for diagnosis, or exploring options.
- Start implementing only when the user explicitly asks to implement, fix, add, change, create, or update something.
- Most sessions begin as evaluation, not implementation. Assess first, then capture the findings in the two-tier todo tracker at the repo root (both tiers are git-ignored — they are the local working tracker, not committed):
  - `todo.md` is the **headline-level index**: one short entry per item (number, title, one-line status), grouped into `Open` and `Done`, each linking to its detail file. Problems only — no solutioning here. This is the running tracker; update item status as work proceeds.
  - `todo/` holds **one detail file per item** (`NN-slug.md`, numbered to match its `todo.md` entry) carrying the in-depth analysis, options, trade-offs, and the proposed/agreed approach. Add sub-findings to the relevant item's detail file, not to `todo.md`.
  - When a cycle's items are all resolved and the tracker is reset for new work, move the finished detail files into `todo/archive/` rather than deleting them; start the new cycle's numbering fresh.
  - Work items one at a time.
- **Discussion-first per item:** when working through a task list, make no code or file changes for an item until its approach has been discussed and explicitly approved. Settle the design in conversation, get sign-off, then implement.
- A design question gets a recommendation and a list of what would change; code follows an explicit go-ahead. A decision the user leaves ("leave it") stays as it is. Work the user defers goes to `todo/BACKLOG.md` with no code change. During live testing, add each fix to `todo/checklist.md` and tick only what was tested live.
- Treat every identified task as priority work. Do not defer with "revisit later" or "doesn't matter now". An item leaves the list only when it is done or the user explicitly rejects it.
- Run this work on Claude Opus at extra-high reasoning (minimum). Evaluation and design decisions here are subtle; do not use a lighter model or lower reasoning effort.

## Project Summary

Sage is a lightweight manager-and-workers micro-PaaS that uses Tailscale for node connectivity, Cloudflare (required) for the domain, DNS and tunnels, Traefik for ingress, Vector for worker logs (the manager logs itself in-process via a SQLite logging handler), Glances for worker metrics (the manager self-collects its own container metrics in-process), SQLite for persistence, APScheduler as a cron/interval trigger feeding an in-memory operation queue, and Vue 3 for the UI.

## Core Context To Preserve

Agents must keep this context active throughout a task, including after summaries, compaction, handoff, or long-running work:

- Manager plus workers over Tailscale is the core architecture.
- Access control is out of scope: the manager API/UI has no authentication and is reached only over the tailnet, whose policy is the only gate. Don't add login, secret masking or ingestion tokens unless asked, and keep the README stating this.
- The manager is a single container with its own userspace Tailscale node (`app/entrypoint.sh`). Required env vars: `TS_AUTH_KEY`, `TS_NAME`, `TS_WORKER_TAG`, `ENCRYPTION_KEY` (can't change after the first boot: startup refuses a different one), `DOMAIN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`; changing them is a restart, and startup converges workers by stamp. S3 and notifications are optional UI settings (`Setting` table). Startup sets up the domain and the manager's certificate before serving (retries, then exits); the UI is only at `https://<TS_NAME>.core.<DOMAIN>`.
- Configuration split: required env vars are cold config the core depends on (Tailscale, Cloudflare, `ENCRYPTION_KEY`) — changing one is a restart. Optional services that don't break the core when missing or wrong (S3, notifications) are hot config, edited in the UI and applied at runtime.
- Services are singleton-based and thread-aware.
- Long or remote-touching operations run through the Manager's in-memory operation queue (`app/utils/queue.py`); entity CRUD is an instant route-side model write (only `delete_container` is queued — it does remote cleanup). APScheduler only fires cron/interval triggers that call `Manager().add_task(...)`.
- `task_id` propagation through `ContextVar` is a core observability requirement.
- Blocking network or remote I/O is offloaded onto the lane pools in `app/utils/executor.py` — `run_in_executor_with_context(...)` (awaited) or `submit_with_context(...)` (fire-and-forget).
- Local SQLite/Peewee work runs inline and relies on WAL mode.
- The scheduler is a thin APScheduler layer (`app/scheduler.py`) that only enqueues; cron jobs fire on wall-clock instants, and a short `_AT_STARTUP` list (release check, certificate renewal, S3 backup discovery) also runs once right after startup.

If this context may have been lost, reread this file plus the relevant `docs/` page before continuing.

## Architecture Map

| Path | Purpose |
| --- | --- |
| `app/entrypoint.sh` | Container entrypoint: starts the manager's userspace `tailscaled`, then the manager process |
| `app/main.py` | Starts the main API, vector ingestion API, and APScheduler scheduler |
| `app/api.py` | Main FastAPI application |
| `app/api_vector.py` | Metrics/log ingestion FastAPI application |
| `app/routes/` | API route handlers |
| `app/services/` | Singleton service layer |
| `app/services/manager/` | Manager singleton (mixins): the operation orchestrator |
| `app/services/db/` | Peewee models and DB bootstrap |
| `app/scheduler.py` | APScheduler cron/interval triggers that enqueue operations |
| `app/utils/queue.py` | In-memory operation queue (scopes + single-dispatcher) |
| `app/utils/executor.py` | Thread-pool lanes + context-preserving offload helpers |
| `app/templates/` | Generated manager and worker runtime files |
| `app/utils/` | Logging, API helpers, encrypted DB utilities |
| `ui/` | Vue 3 frontend |
| `docs/` | Human-facing project docs |

## Non-Negotiable Patterns

### Manager Networking (userspace Tailscale)

- Inbound tailnet traffic to the manager is delivered to `127.0.0.1:<port>` inside the container, so anything listening there (on `0.0.0.0` or `127.0.0.1`) is reachable from the tailnet wherever the tailnet policy allows. Never put an internal-only listener on `127.0.0.1`; use `127.0.0.2` (as tailscaled's outbound proxy does) or a unix socket.
- The container has no route into the tailnet: SSH to workers goes through `tailscale nc` (asyncssh `proxy_command`), HTTP to workers through `TAILNET_HTTP_PROXY`. Internet calls (Cloudflare, S3, ACME, GitHub) go direct.
- Optional services (S3 `s3_config`, notifications) are checked before use: scheduled triggers skip quietly when unconfigured, user actions get a clear 4xx. Cloudflare is always configured; don't add checks for it.

### Services

- Services use the singleton base in `app/services/base.py`.
- Service mutations should remain thread-safe.
- Do not introduce alternate service lifecycle patterns without a clear reason.
- In-memory state derived from the DB or settings (setting values, the S3 client, notification config, the worker summary) is refreshed wherever that data is replaced: a settings save, a restore.
- `GET /api` is polled by every page and relied on by the pause banner and the restore overlay, so it reads memory only: no network, no database.

### Logging And `task_id`

- `task_id` flows through `ContextVar` in `app/utils/logging.py`.
- FastAPI middleware creates the request `task_id`; the queue's `run_task` sets it per queued task.
- The offload helpers copy the context, so worker threads keep the originating `task_id`.
- Do not pass `task_id` through regular function parameters; the one boundary is `add_task(task_id=...)` for route correlation.

### Async Boundaries

- Offload blocking network/remote I/O — never `asyncio.to_thread()` (it bypasses context propagation). Both helpers live in `app/utils/executor.py`:
  - `run_in_executor_with_context(...)` — awaited, from async code; offloads to the task's lane pool (ambient `active_executor`) or an explicit one, and raises if neither is set.
  - `submit_with_context(...)` — fire-and-forget, no running loop required (e.g. notifications).
- A queued task's lane is declared once at `add_task`; offloads inside it inherit it and don't re-name a pool.
- Fan-out orchestrators (`asyncio.gather` over offloads) must be async; never block a sync task on its own lane's pool — it deadlocks a single-worker lane.
- Local Peewee and SQLite calls run inline: SQLite on local disk is not an external call. Heavy file I/O is different (bulk writes in a loop, backup and restore copies, encryption, large listings): offload it, and write in one transaction (`db.atomic()`, `insert_many`) so the loop stays responsive.

### Database

- Main state is stored in SQLite with WAL mode.
- Metrics are stored in per-host SQLite files.
- Logs are stored in per-container SQLite files with FTS5 search.
- Preserve the existing lightweight storage approach unless the task explicitly requires otherwise.

### Migrations And One-Off Changes

- Do not add one-time migration/backfill/convergence code (DB rows, files, or external managed state like Cloudflare DNS records) just to bring existing deployments in line with a change. Prefer documenting the manual step in the summary/PR over shipping code that runs once and is then dead weight.
- The one sanctioned automatic migration is additive DB schema evolution — missing columns are added at boot (see Database). Anything beyond that (data backfills, external-state rewrites) is a manual step unless the user says otherwise.
- New writes should carry the new desired state going forward (e.g. a new default on create); reconciling what already exists is a documented manual action, not code.

### Operation Queue And Scheduler

- APScheduler is a pure cron/interval trigger: each scheduled coroutine only calls `Manager().add_task(...)`. All execution and concurrency control live in the in-memory `TaskQueue` (`app/utils/queue.py`).
- Route and scheduled work alike enqueue via `add_task`; handlers take ids (not ORM objects) and re-fetch. A target row deleted before the task runs is a legitimate interleaving: re-fetch with `get_or_none` and treat a missing row as a quiet no-op (log + return), never a crash.
- Scopes (`platform`, `app`/`app:<qualified_name>`, `common`, `metrics`) give hierarchical mutual exclusion (enforced by the dispatcher); each scope root has its own lane pool. `common` is a single-thread, low-priority lane (settings checks, log cleanup, release refresh): its tasks may wait, so nothing urgent goes there. Admission (drop vs. enqueue) is one `on_conflict` enum on `add_task`: `DEDUP` (default, skip if an identical op — same name + exact scope — is already pending/running, else enqueue and wait), `QUEUE` (always enqueue, no dedupe), `REPLACE` (latest-wins, supersede a pending duplicate then wait). A different op holding a conflicting scope never drops a new task — it defers behind it. `quiet=True` is a separate flag for scheduled background housekeeping: no completed or cancelled row (the Task table is the history of operations worth debugging) and it never makes a settings section "applying". Details in [docs/backend.md](docs/backend.md).
- Task shape: one async task that `asyncio.gather`s its offloaded leaves (independent work in one operation, shared scope), per-entity fan-out (one `add_task` per entity, each with its own scope/lifecycle/DEDUP), or sequential `await`s (ordered/dependent steps). A fan-out parent must be async — never a sync task blocking on its own lane.
- When adding or changing a task, declare its `scopes`, `executor`, and `on_conflict` at the single `add_task` call site and **add/update its row in the Task Catalog** in [docs/backend.md](docs/backend.md). Worker convergence (`sync_workers`/`setup_worker`) holds `{platform}` only — app tasks treat workers as unreliable and fast-fail on offline-flagged ones, so worker churn never barriers app work; `remove_worker`, `refresh_traefik`, and the cert sync keep the broad `{platform, app}` barrier. There is deliberately no per-worker scope dimension — don't add one.
- Scheduler cadence lives in `app/scheduler.py` (`AsyncIOScheduler`): sub-minute pumps use `IntervalTrigger`, everything else uses `CronTrigger.from_crontab`. Gotcha: APScheduler's `from_crontab` treats numeric day-of-week `0` as **Monday**, while the `croniter`-matched per-app volume-backup crons use standard `0` = Sunday — be explicit about the intended weekday. `dispatch_tick` must stay `async` (coroutine jobs run on the loop; a sync job runs in a worker thread where its `asyncio.create_task` has no running loop).
- Sage never restarts or upgrades itself. Pause, restore and stop are a `TaskQueue` state (`Manager().pause_tasks()` etc.): paused, nothing starts but cron work still queues (DEDUP/REPLACE keep one copy each); restoring is paused except for the platform restore itself, and the UI is blocked; a stop signal drains (queued and later-added tasks cancelled, running ones finish). Routes use the Manager, not `scheduler` — only `main.py` imports it. While not running, `app/api.py` refuses changes with 503: a new POST route that only reads must be added to `_READ_ONLY_POSTS` there. See "Pause And Stop" in [docs/backend.md](docs/backend.md).

## Working Rules

1. Read the relevant docs and code before editing.
2. Always read the current state of a file before commenting on whether code will work — never assume based on a previous version in the conversation.
3. Preserve the manager/worker architecture and existing operational assumptions.
4. Prefer small, local changes that match current patterns.
5. Verify task flow, logging flow, and async boundaries when touching orchestration code, including overlapping calls and a cache replaced while another thread reads it.
6. Update docs when behavior or developer workflow changes.
7. Verify, don't assert from memory. Before a claim about a third-party tool, library, or protocol (Traefik, Tailscale, Cloudflare, Peewee, Postgres/redis wire behavior, etc.) drives a design decision or recommendation, confirm it against the current docs, source, or release notes — especially capability claims ("X can't do Y", "the only way is Z") and version-specific behavior. Treat such claims as checkable facts, not recall. State what was verified and link the source; if it cannot be verified, say so explicitly rather than presenting a guess as fact. Check the pinned version in use (e.g. the `traefik:` tag in `docker-compose.yml`), since behavior changes across releases.
8. Scratch and throwaway file operations — cloning external repos, test files, temp scripts, scratch output — go under `/tmp`. Never create them under `/root` or any other real path; only the actual project working tree is edited in place.
9. Don't narrate the old design in code during refactors. Comments must describe the current code and why — no "this used to be X", "vs. the old Y", "the package added an extra level" framing. Explain before/after reasoning in the chat instead. (Domain wording like "worker previously offline" is fine; the rule targets references to the prior implementation/design.) Sage is in development, so every change is the final state of the code: the same holds for docs, this file, release notes and UI text — no dates, sprint or milestone names, "was X before" or migration steps.
10. Don't extract a named helper used in only one place. A single logical piece of work should read top-to-bottom in one place — fragmenting it into single-use helpers forces the reader to jump around (scroll context switching) to follow one flow. Inline short snippets; if two call sites share small behavior, reuse or extend an existing method (e.g. add a flag param) rather than adding a single-use private wrapper.
11. Keep comments minimal — default to none, and add one only when the *why* is non-obvious from the code itself. Comments are change-agnostic: they describe the current state of the code as if it had always been written that way, and never justify or explain the fix being applied (e.g. "everything runs inside the try so an early failure can no longer kill the parent" is fix justification — it belongs in the chat, PR, or commit message, like any why-a-behaviour-changed rationale, migration/recovery scenario, or cross-reference). Don't restate the code, don't pad. When in doubt, leave it out. Before finishing, reread every comment you added and delete it unless the code would mislead without it; reviewers delete the rest.
12. Simplicity means removing incidental complexity, never dropping functionality. A simpler design that loses a capability is not acceptable. Choosing a lighter mechanism for the same behavior is good (e.g. an in-memory queue instead of Redis); removing a behavior because it is fiddly to implement is not (e.g. dropping coalescing/dedup, or rejecting a valid user action, to avoid the work). Preserve the behavior; simplify the mechanism.
13. UI: build from the existing component library (shadcn-vue under `ui/src/components/ui`, add more of its components as needed) rather than hand-rolled HTML and CSS. Don't add a new UI framework or library for one small component. UI text is written for people: a short summary of what will happen or what a state means, not commands, file names or protocol details (those belong in the docs). Forms: a placeholder is only ever an example, written "e.g. …"; a field with a default is prefilled with it; optional fields say "(optional)" in the label. Dates are always shown through `formatDate` / `formatDateStringAgo` (`ui/src/lib/utils.ts`), never raw. The manager's status (queue state, worker summary, settings status) lives in the app store, which owns the one poll; pages poll only their own data. After an action, update the store at once and let the next poll confirm. Every action that returns before the work is done (a queued task) shows its in-progress state at once wherever the server has such a state (application and container status, a settings section applying, the queue state); a new queued action gets the same. State colours come from tones (`lib/tone.ts`) and `StateIndicator`, never palette classes (see [docs/frontend.md](docs/frontend.md)). A refresh or timer stays a single chain when calls overlap (timer, focus, manual).
14. One owner per fact and per piece of logic. A service class owns its facts and the Manager mixin derives from it; variants are data (a table or config tuple such as `CHANNELS`, `APPLYING_TASKS`, a menu item's `indicator`), not special cases in code; a UI component owns its logic end to end (props, not slot plumbing or a separate composable). Before adding a file, store, composable, flag or special case, check whether an existing owner can hold it, and when something general appears, scan for its other users. A new setting, field, status or channel should touch one place.
15. Verification matches the change. Backend: run the code path against a stub or in the dev image and report what was run. UI: when alignment or colour is the point, render it (headless Chrome against the built CSS) instead of reasoning from classes. The user live-tests S3, backup, Cloudflare and Railway flows, so don't build mock S3 servers; say what is left for live testing.

## Token And Context Budget

- Keep this file high-signal; move detailed explanations to `docs/`.
- Prefer links to durable docs over duplicating long explanations.
- Load only the files needed for the current task, then inspect deeper as evidence requires.
- Use `rg` and targeted file reads before broad scans.
- Summarize long findings instead of pasting large file contents into conversation.
- Put task-specific discoveries in the relevant docs only when they become durable project knowledge.

## Build And Verify

Backend commands from `app/`:

```bash
ruff check
autopep8 --in-place --recursive .
isort .
python main.py
```

Frontend commands from `ui/`:

```bash
npm run format:check
npm run format
npm run build
npm run dev
```

Local dev stack (`docker-compose.yml` is dev-only; deploying the image is in the README):

```bash
docker compose up --build --watch
```

## Release Notes

- The release notes are the GitHub release body. Settings → Upgrade shows them as Markdown, after removing HTML comments.
- For every release, generate technical release notes against the previous release: list the files changed since its tag (`git diff <previous tag> --stat`), then read the diffs. Cover config and env vars, startup order, endpoints added and removed, data and file formats (backups, S3 keys), the task queue, the Cloudflare and Tailscale resources Sage creates, the image and CI, and what was tested live and what wasn't. Give names, constants and formats exactly, so a reviewer can check each against the code.
- Put them at the end of the release notes in a collapsible block (`<details><summary>Technical notes</summary> … </details>`). The user-facing notes above it stay short.

## Documentation Rules

- Keep `README.md` as the simple top-level entrypoint.
- Put detail in `docs/` when a topic grows beyond a quick summary.
- Keep `AGENTS.md` focused on project context, constraints, and how to approach tasks.
- Update `AGENTS.md` when durable architecture, workflow, or agent-facing constraints change.
- Keep transient implementation notes out of `AGENTS.md`; use issues, comments, or focused docs instead.

## Don't

- Don't write code when the user only asked for diagnosis or discussion.
- Don't make unnecessary modifications beyond the requested change.
- Don't break `task_id` propagation.
- Don't change scheduler behavior casually.
- Don't replace the SQLite/WAL model with heavier infrastructure without a clear requirement.
- Don't invent architecture that is not already in the repo.
