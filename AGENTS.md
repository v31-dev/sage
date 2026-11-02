# AGENTS

This is a living project-context file for AI coding agents. Keep it concise, current, and focused on durable rules that should survive across sessions, handoffs, and context compaction.

## Start Here

- Read [README.md](README.md) first.
- Read the relevant files under [docs/](docs/) before making changes.
- Read the `AGENTS.md` of the module you are touching: [app/AGENTS.md](app/AGENTS.md) or [ui/AGENTS.md](ui/AGENTS.md).
- Treat `README.md` plus the focused docs as the primary project context.

## Model

- Sessions here run on Claude Opus at `xhigh` effort or above, set with `/model` and `/effort`, because evaluation and design decisions are subtle. Spawn subagents at the same level, never a lighter model or lower effort.

## Project Summary

Sage is a lightweight manager-and-workers micro-PaaS that uses Tailscale for node connectivity, Cloudflare (required) for the domain, DNS and tunnels, Traefik for ingress, Vector for worker logs (the manager stores its queued tasks' lines in-process; its other lines are in stdout, for `docker logs`), Telegraf for worker metrics and container state (the manager self-collects its own container metrics in-process), SQLite for persistence, APScheduler as a cron/interval trigger feeding an in-memory operation queue, and Vue 3 for the UI.

## Core Context To Preserve

These facts hold on every task:

- Manager plus workers over Tailscale is the core architecture.
- Where things run: the manager runs anywhere Docker runs (a laptop, a PaaS, a VM) and needs nothing else; keep it free of host dependencies. A worker is a hardened, bare VM with Docker, dedicated to Sage with no other applications on it, so don't design for workers shared with other software.
- Authentication is out of scope for now: the tailnet and the hosts are assumed secure. The manager API/UI has no login and is reached only over the tailnet, whose policy is the only gate. Don't add login or access control, secret masking, ingestion or agent tokens, agent authentication or SSH host-key pinning unless asked, and keep the README stating that there is no login. Hardening that is not authentication (least-privilege containers, file modes, network isolation) is fine.
- The manager is a single container with its own userspace Tailscale node (`app/entrypoint.sh`). Required env vars: `TS_AUTH_KEY`, `TS_NAME`, `ENCRYPTION_KEY` (can't change after the first boot: startup refuses a different one), `DOMAIN`, `CLOUDFLARE_API_TOKEN` (the Cloudflare account is the one holding `DOMAIN`'s active zone); changing them is a restart, and startup converges workers by stamp. Workers are added by hand (hostname or Tailscale IP); each records the owning instance (`Instance` row) in its `revisions/owner` stamp, and another Sage's worker is refused (it has to be deleted from that Sage first). S3 and notifications are optional UI settings (`Setting` table). Startup sets up the domain and the manager's certificate before serving (retries, then exits); the UI is only at `https://<TS_NAME>.core.<DOMAIN>`.
- Configuration split: required env vars are cold config the core depends on (Tailscale, Cloudflare, `ENCRYPTION_KEY`) — changing one is a restart. Optional services that don't break the core when missing or wrong (S3, notifications) are hot config, edited in the UI and applied at runtime.
- Services are singleton-based and thread-aware.
- Long or remote-touching operations run through the Manager's in-memory operation queue (`app/utils/queue.py`); entity CRUD is an instant route-side model write, except where it needs the remote side: `add_worker`, `remove_worker` and `delete_container` are queued, and `delete_backup` queues its S3 cleanup. APScheduler only fires cron/interval triggers that call `Manager().add_task(...)`.
- `task_id` propagation through `ContextVar` is a core observability requirement.
- Blocking network or remote I/O is offloaded onto the lane pools in `app/utils/executor.py` — `run_in_executor_with_context(...)` (awaited) or `submit_with_context(...)` (fire-and-forget).
- Local SQLite/Peewee work runs inline and relies on WAL mode.
- The scheduler is a thin APScheduler layer (`app/scheduler.py`) that only enqueues; cron jobs fire on wall-clock instants, and the `_AT_STARTUP` list also runs once right after startup.
- State changes (deploys, routing, workers) are infrequent: Sage reacts to them and corrects the config, and up to a minute of lag is fine. Clean design still comes first; the allowance is a reason not to build machinery for per-second precision, not a reason to accept a worse design.

After compaction, reread the relevant `docs/` page before continuing.

## Architecture Map

| Path | Purpose |
| --- | --- |
| `app/entrypoint.sh` | Container entrypoint: starts the manager's userspace `tailscaled`, then the manager process |
| `app/main.py` | Starts the main API, vector ingestion API, and APScheduler scheduler |
| `app/config.py` | Locations, endpoints and timings; read as `config.NAME` where used so tests can override them |
| `app/tests/` | Backend unit tests (`pytest` from `app/`); `conftest.py` provides the `data_dir`, `db` and `register` fixtures |
| `ui/tests/` | UI unit tests (`npm test` from `ui/`, Vitest and happy-dom), in the layout of `ui/src/`; `ui/vitest.config.ts` holds the config |
| `app/api.py` | Main FastAPI application |
| `app/api_ingest.py` | Worker ingestion FastAPI application: Telegraf reports and Vector logs |
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

## Working Rules

1. Preserve the manager/worker architecture and existing operational assumptions.
2. Sage is in development, so every change is the final state of the code, and the no-history rule for comments holds for docs, this file, release notes and UI text too: no dates, sprint or milestone names, "was X before" or migration steps.
3. One owner per fact and per piece of logic. A service class owns its facts and the Manager mixin derives from it; the Manager itself holds only its own bookkeeping (what it decided or asked for: check results, the worker list with the reboots it asked, the stack repairs it started), and a per-worker entry goes when the worker is removed or its time is up; variants are data (a table or config tuple such as `CHANNELS`, `APPLYING_TASKS`, a menu item's `indicator`), not special cases in code; a UI component owns its logic end to end (props, not slot plumbing or a separate composable). Before adding a file, store, composable, flag or special case, check whether an existing owner can hold it, and when something general appears, scan for its other users. A new setting, field, status or channel should touch one place. A file that mixes concepts is split by concept, not by layer, even when that adds files; where ownership and fewer files pull apart, ownership wins.
4. Verification matches the change. Backend: run the code path against a stub or in the dev image and report what was run. UI: when alignment or colour is the point, render it (headless Chrome against the built CSS) instead of reasoning from classes. The user live-tests S3, backup, Cloudflare and Railway flows, so don't build mock S3 servers; say what is left for live testing.
5. Unit tests cover Sage's own code. Every external service (Cloudflare, S3, Tailscale SSH, Docker, ACME, Discord) is replaced by mock data that feeds the code a situation: a failure, a slow answer, a response shape the service documents, or a scenario that does not depend on the service's behaviour. A test asserts a decision our code makes, not what the mock was told to say; if the mock already holds the expected value, the test is not written. The good targets are pure logic (retry policy, classification, decision tables, parsers) and real local state (SQLite, files). What a service really does is left to live testing against the real services. Don't add unit tests for an area the backlog says will be reworked; its tests come with the rework, and ask when unsure.
6. Clean code beats a small gain. A minor cosmetic fix or a jagged optimization that needs machinery (widths computed from character counts, offsets kept in step with a measured height) is left alone, in the UI and the backend alike: say it isn't clean, name the clean partial option if there is one, and don't build it.
7. A stock tool comes before custom code. If an existing tool or a library's built-in behaviour does the job, use it and render its config; a custom agent is the last resort, above all for secondary concerns like logs and metrics. Keep what runs on a worker minimal, treat the manager's cost (CPU, memory, requests per worker) as the budget, and prefer simple code that is easy to maintain over squeezing out memory.
8. A bug a test finds is never parked: no `xfail`, `skip` or any other marker that keeps the suite green. Record it in the todo item, then fix it in the same change, naming the production change to the user, or leave the test failing so nothing merges over it. `skip` is only for a test that cannot run on the machine, such as a missing tool.

## Build And Verify

Backend commands from `app/`:

```bash
ruff check
autopep8 --in-place --recursive .
isort .
pytest
python main.py
```

Frontend commands from `ui/`:

```bash
npm run format:check
npm run format
npm run build
npm run dev
npm test
```

Local dev stack (`docker-compose.yml` is dev-only; deploying the image is in the README):

```bash
docker compose up --build --watch
```

## Release Notes

How to write release notes is in [.claude/skills/release-notes/SKILL.md](.claude/skills/release-notes/SKILL.md).

## Documentation Rules

- Keep `README.md` as the simple top-level entrypoint.
- Put detail in `docs/` when a topic grows beyond a quick summary.
- How the project works goes in `docs/`, for people. What an agent must do or avoid goes in the `AGENTS.md` that owns it: this file for the whole repo, [app/AGENTS.md](app/AGENTS.md) or [ui/AGENTS.md](ui/AGENTS.md) for a module.
- Update the owning `AGENTS.md` when durable architecture, workflow, or agent-facing constraints change.
- Keep transient implementation notes out of `AGENTS.md` files; use issues, comments, or focused docs instead.

## Agent Memory

Agent memory, the todo tracker and the global working rules for this project live in a separate private memory repo. Its path differs per machine, so find it in this order:

1. The path of `rules.md` in your context, if the global rules loaded.
2. The session's working directories, if the user added it to the workspace: a folder holding a `rules.md`.
3. `autoMemoryDirectory` in `.claude/settings.local.json`: the repo is two levels above that folder.
4. A folder named `memory` beside this repository, holding a `rules.md`.

All work in this repo is tracked in the memory repo. A session is set up when the global rules are in your context and your memory directory is `<memory repo>/sage/memory`. If you can't find the repo, ask the user to add it to the workspace or give its path, and wait before starting the task. If the session isn't set up and the repo is in the workspace or the user gave its path, follow Recovery in its `README.md`. If you found it any other way, read its `rules.md`, tell the user in one line and carry on, tracking the work there.
