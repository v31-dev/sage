# AGENTS

This is a living project-context file for AI coding agents. Keep it concise, current, and focused on durable rules that should survive across sessions, handoffs, and context compaction.

## Start Here

- Read [README.md](README.md) first.
- Read the relevant files under [docs/](docs/) before making changes.
- Treat `README.md` plus the focused docs as the primary project context.

## Task Mode

- Default to discussion first when the user is describing a problem, asking for diagnosis, or exploring options.
- Start implementing only when the user explicitly asks to implement, fix, add, change, create, or update something.

## Project Summary

Sage is a lightweight manager-and-workers micro-PaaS that uses Tailscale for node connectivity, Cloudflare for DNS and tunnels, Traefik for ingress, Vector for logs, Glances for metrics, SQLite for persistence, Rocketry for scheduled tasks, and Vue 3 for the UI.

## Core Context To Preserve

Agents must keep this context active throughout a task, including after summaries, compaction, handoff, or long-running work:

- Manager plus workers over Tailscale is the core architecture.
- Services are singleton-based and thread-aware.
- `task_id` propagation through `ContextVar` is a core observability requirement.
- Blocking network or remote I/O uses `run_in_executor_with_context(...)`.
- Local SQLite/Peewee work runs inline and relies on WAL mode.
- Rocketry scheduler behavior must be checked against docs before scheduler or task logging changes.

If this context may have been lost, reread this file plus the relevant `docs/` page before continuing.

## Architecture Map

| Path | Purpose |
| --- | --- |
| `app/main.py` | Starts the main API, vector ingestion API, and Rocketry scheduler |
| `app/api.py` | Main FastAPI application |
| `app/api_vector.py` | Metrics/log ingestion FastAPI application |
| `app/routes/` | API route handlers |
| `app/services/` | Singleton service layer |
| `app/services/db/` | Peewee models and DB bootstrap |
| `app/scheduler.py` | Scheduled and on-demand Rocketry tasks |
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
- FastAPI middleware creates the request `task_id`.
- Rocketry tasks are wrapped so scheduled and API-triggered work also logs with `task_id`.
- Do not pass `task_id` through regular function parameters except at the scheduler trigger boundary that already exists.

### Async Boundaries

- Use `run_in_executor_with_context(...)` for blocking network or remote I/O.
- Do not use `asyncio.to_thread()` here because it will bypass the intended context propagation pattern.
- Local Peewee and SQLite calls are intentionally run inline.

### Database

- Main state is stored in SQLite with WAL mode.
- Metrics are stored in per-host SQLite files.
- Logs are stored in per-container SQLite files with FTS5 search.
- Preserve the existing lightweight storage approach unless the task explicitly requires otherwise.

### Scheduler

- Read Rocketry docs before changing scheduler or task logging behavior.
- Preserve the current `LoggedRocketry` and `LoggedSession` patterns unless the change is deliberate and verified.

## Working Rules

1. Read the relevant docs and code before editing.
2. Always read the current state of a file before commenting on whether code will work — never assume based on a previous version in the conversation.
3. Preserve the manager/worker architecture and existing operational assumptions.
4. Prefer small, local changes that match current patterns.
5. Verify task flow, logging flow, and async boundaries when touching orchestration code.
6. Update docs when behavior or developer workflow changes.

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
