# Development

## Local Setup

The repo includes helper scripts in `dev/`:

- `setup.sh`
  - builds the development environment as your own user and is safe to re-run: each step does
    only what changed
  - Docker: installed from Docker's apt repository on Ubuntu when it is missing (the only step
    that uses `sudo`); on another OS it only says so
  - Python: a pinned `uv`, Python 3.14 (uv installs it, so the system Python doesn't matter), the virtual
    environment under `app/.venv` and the requirements with the dev tools; the environment is
    recreated when its Python is missing or the wrong version
  - Node: `nvm`, the Node version in `ui/.nvmrc`, and `ui/node_modules` (`npm ci`, repeated
    when `package.json`, `package-lock.json` or the Node version changes)
  - afterwards, in a new shell: `source app/.venv/bin/activate`, and `cd ui && nvm use`
- `test-app.sh`
  - runs the backend tests in a `python:3.14-slim` container, for a machine without Python 3.14;
    see Tests
- `test-ui.sh`
  - runs the UI tests in a Node container on the major in `ui/.nvmrc`, for a machine without that
    Node; see UI Tests
- `env.sh`
  - resets the local compose environment: wipes the manager's `data` volume except its
    Tailscale state and issued cert

These scripts assume a fairly direct machine setup and should be reviewed before use on an existing environment.

## Environment

The manager's env vars are all required (see the README): `TS_AUTH_KEY`, `TS_NAME`,
`TS_WORKER_TAG`, `ENCRYPTION_KEY` (fixed after the first boot — wipe `SAGE_DATA` to switch
keys) and the Cloudflare details `DOMAIN`, `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_API_TOKEN`. Use a domain only the dev manager uses: two managers on one domain
share its tunnel and records.

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
process. A node that can't join the tailnet (a bad key, or no address after 30 seconds)
stops the container with a `[CRITICAL]` line in Sage's log format, after stopping
`tailscaled` so that its output doesn't bury it. The container exits when either process does. No host Tailscale socket, TUN
device or published ports are needed; tailnet traffic to the node's ports reaches the
container's listeners.

Workers run their own edge stack (`traefik`, `vector`, `glances` and `cloudflared`), bootstrapped remotely over Tailscale SSH.

### Dev routing

The dev manager is its own tailnet node too. Give it its own `TS_AUTH_KEY` (tagged, e.g.
`tag:manager-dev`) and `TS_NAME`.

Nothing is published on the host; everything is reached over the tailnet:

- `https://<TS_NAME>.core.<DOMAIN>` — the UI with hot reload. The dev `traefik` service
  shares the `sage` container's network namespace (`network_mode: service:sage`), so it
  owns the dev manager's `:80` and `:443`. It serves the manager's certificate (from
  `SAGE_DATA/certs/manager`), routes `/api` to the manager (plain HTTP on `:9000` in dev)
  and everything else, including Vite's HMR WebSocket, to the `ui` service (Vite on
  `:5173`); `:80` and other hostnames don't serve the UI. Config: `dev/traefik/`
  (templated with `TS_NAME` and `DOMAIN`). Compose restarts it whenever `sage` restarts,
  since it loses its network with it. It doesn't watch the certificate files: after the
  manager's certificate renews (about every 60 days), run `docker compose restart traefik`.
- `<TS_NAME>:5678` — debugpy. The VS Code "Attach to dev manager" configuration prompts
  for the address. debugpy has no authentication, so only allow your own devices to reach
  this port in the tailnet policy.

The tailnet policy must let your devices reach the dev manager on `:80` and `:443` (and `:5678` for
debugging).

## Backend Commands

Run from `app/`:

```bash
ruff check
autopep8 --in-place --recursive .
isort .
pytest
python main.py
```

## Tests

The backend's unit tests are in `app/tests/`; run `pytest` from `app/` on Python 3.14 (from the
repo root, `pytest app/tests`; the config is in `app/pyproject.toml`).
`setup.sh` installs `requirements-dev.txt`, which includes `requirements-test.txt` (pytest,
pytest-asyncio, pytest-cov, ruff and isort, the tools CI runs).

Without Python 3.14 on the machine, `dev/test-app.sh` runs the same tests in a Docker
container and passes its arguments to pytest (`dev/test-app.sh -q --no-cov -k queue`). It keeps
the virtual environment in `~/.cache/sage-test-venv` (or `SAGE_TEST_VENV`) and rebuilds it when the
requirements change; the first run takes about a minute, later runs a second or two.

The suite runs in `Asia/Kolkata` (UTC+5:30), set in `tests/conftest.py`, so a time read from the local clock
fails a test. The `clock` fixture freezes `utcnow()` (`clock.set(...)`, `clock.advance(...)`) and
`machine_timezone("America/Los_Angeles")` moves a single test to another zone.

`pytest` measures coverage by default (`addopts` in `app/pyproject.toml`) and prints the table with
the missing lines; `pytest --no-cov` turns it off, which is what to use under a debugger. Every
backend module is measured, including the ones no test imports; only `main.py` (it starts the
Manager when imported) and the tests are left out. CI reports it in the job summary and uploads
`coverage.xml` as an artifact; it doesn't fail a build on a coverage level.

`-q` (quiet) only shortens pytest's own output to the dots and the summary line; `-v` lists each
test by name.

The tests need no container, Tailscale or network. `tests/conftest.py` sets the env vars the app
reads at import, clears the service singletons between tests, and provides:

- `data_dir`: points `config.DATA_DIR` and `config.DB_PATH` at a temporary directory;
- `db`: a real `Database` on that directory, with every table created;
- `register(cls, fake)`: puts a fake where a service singleton would be built.

Locations, endpoints and timings live in `app/config.py` and are read as `config.NAME` where they
are used, so a test overrides one attribute (`monkeypatch.setattr(config, ...)`).

A bug fix comes with a regression test: reproduce the bug in a test and see it fail, fix the code,
and keep the test so the bug can't return unnoticed.

A known bug is never parked behind `xfail`: `tests/conftest.py` reports an expected failure as a
failure, so the run stays red until the bug is fixed. pytest lists every failed and skipped test
with its reason in the summary by default (`-ra`).

## UI Tests

The UI's tests are in `ui/tests/`, in the layout of `ui/src/`; run `npm test` from `ui/`. They use
Vitest with happy-dom as the DOM and Vue Test Utils for mounting, and the config is in
`ui/vitest.config.ts`, apart from `vite.config.ts` so the production build never reads it.
`npm test` measures coverage by default (V8) and prints a summary; `npm run test:watch` runs
without it and reruns on a change. Every file in `src/` counts, also the ones no test imports; the
per-file report is in `ui/coverage/lcov-report/index.html`. CI reports it in the job summary and
uploads `ui/coverage` as an artifact; it doesn't fail a build on a coverage level.

The tests run in UTC and `en-US` whatever the machine is set to, and the API client does not wait
before a call (`VITE_LOAD_DELAY=0`). `fetch` is replaced in each test, and a store test mocks
`fetchAppInfo`. `npm run typecheck:tests` type-checks the tests, which `npm run build` does not.
So far the tests cover the pure logic: `src/lib/`, `src/services/api.ts`, the Pinia store and the
route table. Components are not tested yet.

Without the pinned Node on the machine, `dev/test-ui.sh` runs the same tests in a Docker container
and passes its arguments to vitest (`dev/test-ui.sh tests/lib --coverage.enabled=false`). It keeps
`node_modules` in `~/.cache/sage-test-node-modules` (or `SAGE_TEST_NODE_MODULES`), apart from
`ui/node_modules`, and reinstalls when `package.json`, `package-lock.json` or the image change;
the first run takes under a minute, later runs a second or two.

## Frontend Commands

Run from `ui/`:

```bash
npm run format:check
npm run format
npm run build
npm run dev
npm test
npm run test:watch
npm run typecheck:tests
```

## Docker Image

The top-level `Dockerfile` builds:

1. the UI bundle, on the Node major in `ui/.nvmrc` (`setup.sh`, the CI UI job and both Dockerfiles
   use it; a Dockerfile can't read the file, so its `node:<major>-alpine` tag is changed by hand
   with it)
2. the Python app image that serves the built frontend from `/app/static` and exposes it at `/` while leaving `/api` on the backend

The production UI build currently injects `VITE_LOAD_DELAY=0`, while local frontend development keeps the source default delay.

## CI And Release Notes

Current GitHub workflows:

- `ci.yml`
  - runs on every pull request; a newer run on the same ref cancels the older one
  - two independent jobs that run in parallel
  - backend: `ruff check`, `isort --check-only .` and `pytest` with coverage on Python 3.14
  - UI: `npm ci`, `npm run format:check`, `npm run build`, `npm run typecheck:tests` and `npm test` with
    a coverage summary and artifact, on the Node version in `ui/.nvmrc`
  - the actions are pinned by commit SHA
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
