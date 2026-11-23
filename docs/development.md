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
`ENCRYPTION_KEY` (fixed after the first boot — wipe `SAGE_DATA` to switch
keys) and the Cloudflare details `DOMAIN` and
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

Workers run their own edge stack (`traefik`, `vector`, `telegraf` and `cloudflared`), bootstrapped remotely over Tailscale SSH.

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
  manager's certificate renews, run `docker compose restart traefik`.
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

Unit tests cover Sage's own code. Cloudflare, S3, Tailscale SSH, Docker, ACME and Discord are replaced
by mock data that feeds the code a situation (a failure, a slow answer, a documented response shape);
what the services really do is left to live testing against the real ones.

The suite runs in `Asia/Kolkata` (UTC+5:30), set in `tests/conftest.py`, so a time read from the local clock
fails a test. The `clock` fixture freezes `utcnow()` (`clock.set(...)`, `clock.advance(...)`) and
`machine_timezone("America/Los_Angeles")` moves a single test to another zone.

`pytest` measures coverage by default (`addopts` in `app/pyproject.toml`) and prints the table with
the missing lines; `pytest --no-cov` turns it off, which is what to use under a debugger. Every
backend module is measured, including the ones no test imports; only `main.py` (it starts the
Manager when imported) and the tests are left out. CI reports it in the job summary and uploads
`coverage.xml` as an artifact; it doesn't fail a build on a coverage level.

Every warning is an error (`filterwarnings` in `app/pyproject.toml`): a deprecation, a library's included, fails the
test it is raised in.

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

### Vector Config

CI has no Vector, so the unit tests only check the structure of
`app/templates/worker/vector/vector.yml`. A change to its transforms is checked with the image the
workers run, pinned in `app/templates/worker/docker-compose.yml`. Copy the template into a scratch
folder and append cases to the copy:

```yaml
tests:
  - name: an application's line keeps only the stored fields and names its application
    inputs:
      - insert_at: shipped
        type: log
        log_fields:
          container_name: sage-0123456789abcdef
          label."sage.container": "0123456789abcdef"
          label."sage.application": demo-web
          stream: stdout
          message: hello
          extra: dropped
    outputs:
      - extract_from: shipped
        conditions:
          - type: vrl
            source: |
              assert_eq!(.application, "demo-web")
              assert!(!exists(.extra))
  - name: a container without the container label is dropped
    inputs:
      - insert_at: shipped
        type: log
        log_fields:
          container_name: other
          message: hello
    no_outputs_from: [shipped]
```

Run them from that folder, then check the whole config without connecting to Docker or the sink:

```bash
docker run --rm -e IP=127.0.0.1 -e INGEST_PORT=9001 -e HOSTNAME=w1 -v "$PWD":/etc/vector:ro timberio/vector:<tag> test /etc/vector/vector.yml
docker run --rm -e IP=127.0.0.1 -e INGEST_PORT=9001 -e HOSTNAME=w1 -v "$PWD":/etc/vector:ro timberio/vector:<tag> validate --no-environment /etc/vector/vector.yml
```

## UI Tests

The UI's tests are in `ui/tests/`, in the layout of `ui/src/`; run `npm test` from `ui/`. They use
Vitest with happy-dom as the DOM and Vue Test Utils for mounting, and the config is in
`ui/vitest.config.ts`, apart from `vite.config.ts` so the production build never reads it.
`npm test` measures coverage by default (V8) and prints a summary; `npm run test:watch` runs
without it and reruns on a change. Every file in `src/` counts, also the ones no test imports; the
per-file report is in `ui/coverage/lcov-report/index.html`. CI reports it in the job summary and
uploads `ui/coverage` as an artifact; it doesn't fail a build on a coverage level.

The tests run in UTC and `en-US` whatever the machine is set to, and `request` does not wait
before a call (`VITE_LOAD_DELAY=0`). `fetch` is replaced in each test, and a store test mocks
`fetchAppInfo`. `npm run typecheck:tests` type-checks the tests, which `npm run build` does not.
The tests cover the pure logic (`src/lib/`, `src/services/api/`, the Pinia store, the route table) and
the pages and components.

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

`VITE_LOAD_DELAY` (milliseconds, 300 when unset) makes `request` (`ui/src/lib/api.ts`) wait before every call, so that development simulates a slow network: the instant status updates after an action and the protection against a second click can be seen and tried. Raise it (for example `VITE_LOAD_DELAY=2000`) to test them harder, and set it to 0 to judge how responsive the UI really is. The production UI build and the UI tests set it to 0.

## Migrations

An install moves from any supported version to any later one by running the newer image on the same volume: the boot
migrates what the older version left. The rules are in [app/AGENTS.md](../app/AGENTS.md#migrations).

**Database.** `PRAGMA user_version` is the number of the last step applied, and the steps are one ordered list in
`app/services/db/migrations.py`, run by the runner every SQLite file shares (`app/services/db/migrator.py`). A database at version 0 is the baseline, 0.1.3, and must have exactly its schema; an
older one is refused, and running 0.1.3 on it once brings it to the baseline. A database at a version above the
latest is refused too, so going back to an earlier release after an upgrade means restoring the backup taken before
it, or putting back the copy below. At boot every pending step runs in order, then the schema is checked against the
models, all in one transaction: a failure anywhere leaves the database as it was, at its old version. Before a boot
runs a step, the runner copies the database beside it as `data.db.migration-<its version>` (0 is the baseline),
replacing a copy of that name, and the run's log names it; a copy that can't be written stops the boot. Put back as
`data.db`, a copy is the database as the release that wrote it left it. Several migrations leave several copies, and
the daily cleanup removes those older than `config.MIGRATION_COPY_RETENTION_DAYS` (90). A platform restore takes
none: its backup in the store is the copy.

To change the schema, add the next step: SQL that takes the previous version's schema to the new one and carries the
rows across, written without the models (they describe only the latest version). A release can ship no step or
several. The migration test builds a 0.1.3 database with rows in every table, runs every step and requires the result
to equal a fresh database from the models, so a model change without its step fails the suite.

**During development.** A pull request adds at most one step, its schema version, whatever release it ships in: while
the PR is open, each further schema change goes into that step, edited in place, since no install has run it yet. A
local database that already ran an earlier draft of the step is refused at boot, its schema no longer matching the
models; how to bring it back depends on the change, case by case (put back a copy from before it ran, or start the
local data afresh).

**Fixing a released step.** A step that fails at boot was rolled back, so the install is still on its old version and
the previous release runs on it. The fix goes out in a patch release, and the user upgrades to it straight from the
version they are on: say 0.3 ships steps 2 and 3, step 3 fails on some data, and 0.3.1 fixes step 3 in place. An
install on 0.2 (version 1) runs steps 2 and 3, as 0.3.1 has them, in one go, and the schema is checked once, after
the last step. Fixing a step in place is right only while its result stays the same, since the installs it already
ran on hold that result. A step that succeeded with a wrong result is corrected by a new step.

**Side files.** Each kind of side SQLite file (`logs/<application>.db`, `logs/requests.db`, `logs/tasks.db`, the
metrics shards) carries its own `user_version`, with its baseline and steps in its package's `migrations.py`
(`services/logs/`, `services/metrics/`), under
the same rules; `Logs` and `Metrics` migrate every existing file when they start, before Sage serves. They hold history
only, so a file that doesn't fit (version 0 without the baseline, or a later version) is recreated empty, and a step
can be `migrator.RECREATE`, which starts the file empty at the latest version, where a change can't carry the rows
cleanly. A file whose migration fails is recreated empty too, and so is one that isn't a sound database (SQLite's
"not a database" or "malformed"), with its WAL files, so a side file never stops the boot. `data.db` migrates first and
is the only file that can stop it, before anything else has changed; files are migrated one at a time, since SQLite
commits attached databases together only outside WAL mode.

**Workers, the store and Cloudflare.** What Sage keeps outside its databases converges through its owner: a format
change on a worker changes its stamp, so worker setup rewrites it, and an artifact an older version left is removed by
its owner's convergence. Nothing outside the databases is converted: an artifact in a form the new version no longer
reads (an S3 key, a file name, a DNS record) may be dropped, left behind or removed as an orphan, and the release notes
name what is dropped.

**History.** Each run, a boot's or a platform restore's install, has a log of its own,
`data/migrations/migration-<run>.log` (`app/services/db/migration_log.py`): a first line naming the run, its trigger
and release, then a JSON line for each line logged, kept whole. It is outside the databases, since a refused run rolls
its database back. While a file migrates, the runner keeps every line its thread logs (time, level, message, at the
configured `LOG_LEVEL`), so each file's versions and outcome are in the log itself ("Migrated [...] from version [0] to
[1]", "Recreating [...] empty: ...", "Refused [...]: ...") and a step tells what it did by logging it, with ids and
names in `[]`. A step can also return the manual actions its change leaves, each with where to take it (a worker,
Cloudflare, the store, Sage), the exact command when there is one and why; the step builds them while it has both the
old rows and the new ids, and they go in the log as CRITICAL lines, its command between `###` lines. `GET
/api/migrations` gives the runs, newest first, each with whether a file was refused, how many actions it left and its
`lines`, the actions last. Settings → Upgrade shows them in its Migration logs dialog, a log like a task's, with a note
above it when a run left steps to do by hand, so a refused upgrade is visible once the previous release runs again. The manager also keeps the
latest run of its own start in memory (`MigrationRuns`), a boot's or a platform restore's, and `GET /api` carries it as
`migration`: a run that left steps to do by hand opens the Upgrade page on it once in each browser. Each such run is
also a notification, sent once the Manager is set up or the restore is done, saying what it brought the data up to and
how many steps it left, with a link to `/upgrade?migration=<run>`, which opens the Migration logs on that run.

**Release notes.** A manual upgrade step goes in the release notes only in an emergency the automatic migration can't
solve; the boot's refusal links the release page, so its notes can be corrected without a new release.

**Moving the baseline.** Raising the baseline to a later release removes the steps and convergence code that only
older installs need; databases from before the new baseline are then refused, with the new baseline as their way
forward.

## CI And Release Notes

Current GitHub workflows:

- `ci.yml`
  - runs on every pull request that is not a draft, and when a draft is marked ready for review; a newer run on the
    same ref cancels the older one
  - two independent jobs that run in parallel
  - backend: `ruff check`, `isort --check-only .`, `autopep8 --diff --recursive --exit-code .` and `pytest` with coverage on Python 3.14
  - UI: `npm ci`, `npm run format:check`, `npm run build`, `npm run typecheck:tests` and `npm test` with
    a coverage summary and artifact, on the Node version in `ui/.nvmrc`
- `release-pr.yml`
  - runs manually through `workflow_dispatch`
  - requires the workflow to be started from `main`
  - bumps the root `VERSION` file by `patch`, `minor`, or `major`
  - creates a `release/v<VERSION>` branch with that change
  - opens a release PR titled `v<VERSION>`
  - fills the PR body with the generated GitHub release notes and a `## Developer Notes` section holding `None`, which
    is replaced by hand when a release needs notes
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

The actions in all three workflows are pinned by commit SHA, with the version in a trailing comment.

## Documentation Notes

This documentation set is based on the current repository scan. If runtime behavior changes, update these docs together with the corresponding code.
