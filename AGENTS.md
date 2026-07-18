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
- Treat every identified task as priority work. Do not defer with "revisit later" or "doesn't matter now". An item leaves the list only when it is done or the user explicitly rejects it.
- Run this work on Claude Opus at extra-high reasoning (minimum). Evaluation and design decisions here are subtle; do not use a lighter model or lower reasoning effort.

## Project Summary

Sage is a lightweight manager-and-workers micro-PaaS that uses Tailscale for node connectivity, Cloudflare for DNS and tunnels, Traefik for ingress, Vector for logs, Glances for worker metrics (the manager self-collects its own container metrics in-process), SQLite for persistence, APScheduler as a cron/interval trigger feeding an in-memory operation queue, and Vue 3 for the UI.

## Core Context To Preserve

Agents must keep this context active throughout a task, including after summaries, compaction, handoff, or long-running work:

- Manager plus workers over Tailscale is the core architecture.
- Services are singleton-based and thread-aware.
- Long or remote-touching operations run through the Manager's in-memory operation queue (`app/utils/queue.py`); entity CRUD is an instant route-side model write (only `delete_container` is queued — it does remote cleanup). APScheduler only fires cron/interval triggers that call `Manager().add_task(...)`.
- `task_id` propagation through `ContextVar` is a core observability requirement.
- Blocking network or remote I/O is offloaded onto the lane pools in `app/utils/executor.py` — `run_in_executor_with_context(...)` (awaited) or `submit_with_context(...)` (fire-and-forget).
- Local SQLite/Peewee work runs inline and relies on WAL mode.
- The scheduler is a thin APScheduler layer (`app/scheduler.py`) that only enqueues; cron jobs fire on wall-clock instants (never at boot).

If this context may have been lost, reread this file plus the relevant `docs/` page before continuing.

## Architecture Map

| Path | Purpose |
| --- | --- |
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

### Services

- Services use the singleton base in `app/services/base.py`.
- Service mutations should remain thread-safe.
- Do not introduce alternate service lifecycle patterns without a clear reason.

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
- Local Peewee and SQLite calls are intentionally run inline.

### Database

- Main state is stored in SQLite with WAL mode.
- Metrics are stored in per-host SQLite files.
- Logs are stored in per-container SQLite files with FTS5 search.
- Preserve the existing lightweight storage approach unless the task explicitly requires otherwise.

### Operation Queue And Scheduler

- APScheduler is a pure cron/interval trigger: each scheduled coroutine only calls `Manager().add_task(...)`. All execution and concurrency control live in the in-memory `TaskQueue` (`app/utils/queue.py`).
- Route and scheduled work alike enqueue via `add_task`; handlers take ids (not ORM objects) and re-fetch. A target row deleted before the task runs is a legitimate interleaving: re-fetch with `get_or_none` and treat a missing row as a quiet no-op (log + return), never a crash.
- Scopes (`platform`, `app`/`app:<qualified_name>`, `common`, `metrics`) give hierarchical mutual exclusion (enforced by the dispatcher); each scope root has its own lane pool. Admission (drop vs. enqueue) is one `on_conflict` enum on `add_task`: `DEDUP` (default, skip if an identical op — same name + exact scope — is already pending/running, else enqueue and wait), `QUEUE` (always enqueue, no dedupe), `REPLACE` (latest-wins, supersede a pending duplicate then wait). A different op holding a conflicting scope never drops a new task — it defers behind it. `quiet=True` is a separate flag for high-frequency reconcilers (record only on failure). Details in [docs/backend.md](docs/backend.md).
- Task shape: one async task that `asyncio.gather`s its offloaded leaves (independent work in one operation, shared scope), per-entity fan-out (one `add_task` per entity, each with its own scope/lifecycle/DEDUP), or sequential `await`s (ordered/dependent steps). A fan-out parent must be async — never a sync task blocking on its own lane.
- When adding or changing a task, declare its `scopes`, `executor`, and `on_conflict` at the single `add_task` call site and **add/update its row in the Task Catalog** in [docs/backend.md](docs/backend.md). Worker convergence (`sync_workers`/`setup_worker`) holds `{platform}` only — app tasks treat workers as unreliable and fast-fail on offline-flagged ones, so worker churn never barriers app work; `remove_worker`, `refresh_traefik`, and the cert sync keep the broad `{platform, app}` barrier. There is deliberately no per-worker scope dimension — don't add one.
- Scheduler cadence lives in `app/scheduler.py` (`AsyncIOScheduler`): sub-minute pumps use `IntervalTrigger`, everything else uses `CronTrigger.from_crontab`. Gotcha: APScheduler's `from_crontab` treats numeric day-of-week `0` as **Monday**, while the `croniter`-matched per-app volume-backup crons use standard `0` = Sunday — be explicit about the intended weekday. `dispatch_tick` must stay `async` (coroutine jobs run on the loop; a sync job runs in a worker thread where its `asyncio.create_task` has no running loop).

## Working Rules

1. Read the relevant docs and code before editing.
2. Always read the current state of a file before commenting on whether code will work — never assume based on a previous version in the conversation.
3. Preserve the manager/worker architecture and existing operational assumptions.
4. Prefer small, local changes that match current patterns.
5. Verify task flow, logging flow, and async boundaries when touching orchestration code.
6. Update docs when behavior or developer workflow changes.
7. Verify, don't assert from memory. Before a claim about a third-party tool, library, or protocol (Traefik, Tailscale, Cloudflare, Peewee, Postgres/redis wire behavior, etc.) drives a design decision or recommendation, confirm it against the current docs, source, or release notes — especially capability claims ("X can't do Y", "the only way is Z") and version-specific behavior. Treat such claims as checkable facts, not recall. State what was verified and link the source; if it cannot be verified, say so explicitly rather than presenting a guess as fact. Check the pinned version in use (e.g. the `traefik:` tag in `docker-compose.yml`), since behavior changes across releases.
8. Scratch and throwaway file operations — cloning external repos, test files, temp scripts, scratch output — go under `/tmp`. Never create them under `/root` or any other real path; only the actual project working tree is edited in place.
9. Don't narrate the old design in code during refactors. Comments must describe the current code and why — no "this used to be X", "vs. the old Y", "the package added an extra level" framing. Explain before/after reasoning in the chat instead. (Domain wording like "worker previously offline" is fine; the rule targets references to the prior implementation/design.)
10. Don't extract a named helper used in only one place. A single logical piece of work should read top-to-bottom in one place — fragmenting it into single-use helpers forces the reader to jump around (scroll context switching) to follow one flow. Inline short snippets; if two call sites share small behavior, reuse or extend an existing method (e.g. add a flag param) rather than adding a single-use private wrapper.
11. Keep comments minimal — default to none, and add one only when the *why* is non-obvious from the code itself. Comments are change-agnostic: they describe the current state of the code as if it had always been written that way, and never justify or explain the fix being applied (e.g. "everything runs inside the try so an early failure can no longer kill the parent" is fix justification — it belongs in the chat, PR, or commit message, like any why-a-behaviour-changed rationale, migration/recovery scenario, or cross-reference). Don't restate the code, don't pad. When in doubt, leave it out.
12. Simplicity means removing incidental complexity, never dropping functionality. A simpler design that loses a capability is not acceptable. Choosing a lighter mechanism for the same behavior is good (e.g. an in-memory queue instead of Redis); removing a behavior because it is fiddly to implement is not (e.g. dropping coalescing/dedup, or rejecting a valid user action, to avoid the work). Preserve the behavior; simplify the mechanism.

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

Compose commands:

```bash
docker compose up -d
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

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
