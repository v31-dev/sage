# Development

## Local Setup

The repo includes helper scripts in `setup/dev/`:

- `ubuntu.sh`
  - installs Docker
  - creates the Python virtual environment under `app/app-venv`
  - installs Python dependencies
  - installs Node via `nvm`
  - installs UI dependencies
- `env.sh`
  - resets the local compose environment: wipes the manager's `data` volume except its
    Tailscale state and issued cert

These scripts assume a fairly direct machine setup and should be reviewed before use on an existing environment.

## Environment

The manager takes two env vars, both required: `TS_AUTH_KEY` (a tagged, non-ephemeral
Tailscale auth key, used only while the node has no tailnet identity) and `TS_NAME` (its
tailnet machine name, also its name inside Sage). The encryption key is generated on first
boot as `/app/data/encryption.key`; database backups are unreadable without it.

For local development, copy [`sample.env`](../sample.env) to `.env`. It adds `SAGE_DATA`,
the host directory mounted at `/app/data` (`./data-gitignore`, git-ignored).

## Running The Stack

The published image runs as a single container; deployment (`docker run`, Compose) is
described in the [README](../README.md#usage). The repo's
`docker-compose.yml` is for local development only:

```bash
docker compose up --build --watch
```

- `--watch` syncs `ui/src` into the Vite container (`develop.watch`), which is what drives
  hot reload.
- `--build` rebuilds the images. The manager's code (`app/`) is deliberately not watched:
  after a backend change, stop the stack and run the command again. The same applies to
  the UI's `vite.config.ts` / `package.json`.
- To debug the manager, run the stack and attach VS Code's "Attach to dev manager"
  configuration (debugpy at `<TS_NAME>:5678`, see below).

It builds the manager from `app/Dockerfile` (`ENV=development`, debugpy on `:5678`,
`SYS_PTRACE` for profiling) and adds the `ui` and dev `traefik` services described below.

The manager container runs the API/UI (plain HTTP on `:80`; tailnet traffic is already
encrypted by WireGuard), metrics and log capture.

The container joins the tailnet as its own node: `app/entrypoint.sh` starts `tailscaled`
in userspace networking mode (state in `/app/data/tailscale`, `TS_DEBUG_MTU=1230`), logs in
with `TS_AUTH_KEY` only when the node has no tailnet identity yet, then starts the Python
process. The container exits when either process does. No host Tailscale socket, TUN
device or published ports are needed; tailnet traffic to the node's ports reaches the
container's listeners.

Workers run their own edge stack (`cloudflared`, `traefik`, `vector`, `glances`), bootstrapped remotely.

### Dev routing

The dev manager is its own tailnet node too. Give it its own `TS_AUTH_KEY` (tagged, e.g.
`tag:manager-dev`) and `TS_NAME`.

Nothing is published on the host; everything is reached over the tailnet:

- `http://<TS_NAME>` — the UI with hot reload. The dev `traefik` service shares the `sage`
  container's network namespace (`network_mode: service:sage`), so it owns the dev
  manager's `:80`. It routes `/api` to the manager (plain HTTP on `:9000` in dev) and
  everything else, including Vite's HMR WebSocket, to the `ui` service (Vite on `:5173`).
  Config: `setup/dev/traefik/`. Compose restarts it whenever `sage` restarts, since it
  loses its network with it.
- `<TS_NAME>:5678` — debugpy. The VS Code "Attach to dev manager" configuration prompts
  for the address. debugpy has no authentication, so only allow your own devices to reach
  this port in the tailnet policy.

The tailnet policy must let your devices reach the dev manager on `:80` (and `:5678` for
debugging).

## Backend Commands

Run from `app/`:

```bash
ruff check
autopep8 --in-place --recursive .
isort .
python main.py
```

## Frontend Commands

Run from `ui/`:

```bash
npm run format:check
npm run format
npm run build
npm run dev
```

## Docker Image

The top-level `Dockerfile` builds:

1. the UI bundle
2. the Python app image that serves the built frontend from `/app/static` and exposes it at `/` while leaving `/api` on the backend

The production UI build currently injects `VITE_LOAD_DELAY=0`, while local frontend development keeps the source default delay.

## CI And Release Notes

Current GitHub workflows:

- `release-pr.yml`
  - runs manually through `workflow_dispatch`
  - requires the workflow to be started from `main`
  - bumps the root `VERSION` file by `patch`, `minor`, or `major`
  - creates a `release/v<VERSION>` branch with that change
  - opens a release PR titled `v<VERSION>`
  - fills the PR body with the generated GitHub release notes
  - adds a changelog compare link as a PR comment for reviewer use
  - applies the combined labels from the merged PRs since the previous release
- `publish.yml`
  - runs on pushes to `main` that change `VERSION`
  - validates that the merged pull request associated with the pushed commit is titled `v<VERSION>`
  - builds and publishes a GHCR image from that exact version
  - tags the image with the exact `VERSION` and moves `latest` to it
  - creates a GitHub release with generated release notes configured by `.github/release.yml`
  - uses the highest existing `vMAJOR.MINOR.PATCH` tag lower than the current release as the previous release-note boundary when available
  - generates release notes from the changes on `main` before the release PR merge commit so the release PR itself is not included as a changelog entry

## Documentation Notes

This documentation set is based on the current repository scan. If runtime behavior changes, update these docs together with the corresponding code.
