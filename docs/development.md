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
  - loads `.env`
  - resets the local compose environment
  - refreshes `HOSTNAME` and `TS_IP`

These scripts assume a fairly direct machine setup and should be reviewed before use on an existing environment.

## Environment

`sample.env` shows the current expected variables. Important groups are:

- local runtime paths and host identity
- Cloudflare credentials and domain metadata
- encryption key
- published Docker image tag selection through `SAGE_IMAGE_TAG`
- optional notification settings
- optional S3 backup settings

## Running The Stack

Docker Compose is the only supported deployment method. The in-UI self-upgrade
(`POST /settings/upgrade`) launches a detached `docker:cli` updater container that
runs `docker compose pull`/`up -d` against the manager's own compose file and
`.env` — it discovers that host-side location from the running container's
`com.docker.compose.*` labels, and refuses to run when they are absent (i.e. a
standalone `docker run` deployment) or when `ENV=development` (the override builds
from source rather than pulling).

Production-style compose:

```bash
docker compose up -d
```

The production compose image requires `SAGE_IMAGE_TAG`. Keep `sample.env` aligned with `VERSION`; the publish workflow updates both together during a release. The updater rewrites `SAGE_IMAGE_TAG` in `.env` on a successful upgrade (and reverts it on a failed one).

Development compose:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

The production compose stack runs a single manager container:

- `sage` — the API/UI, the `:443` TLS endpoint, in-process ACME, metrics, and log capture

Workers run their own edge stack (`cloudflared`, `traefik`, `vector`, `glances`), bootstrapped remotely.

### Dev routing

The prod manager serves `:443` itself. In dev the override keeps the same URL (`https://sage.core.<domain>`) **and** Vite HMR by adding two more services and a dev-only Traefik:

- `ui` — the Vite dev server (`:5173`, HMR).
- `traefik` (`sage-dev-traefik`) — terminates TLS on `:443` using the cert **sage still issues in-process** (mounted PEM, file provider; no ACME in Traefik) and routes by container labels: `Host(sage.core.<domain>) && PathPrefix(/api)` → `sage:9000`, everything else → `ui:5173`.

To avoid a host `:443` conflict, the override replaces sage's port list (`ports: !override`) to drop sage's own `:443` mapping, and `app/main.py` skips its TLS server when `ENV=development` (the cert is still issued for Traefik to serve). HMR rides the same `:443` (`vite.config.ts` sets `server.hmr.clientPort = 443`).

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

- `pr-check.yml`
  - runs when a pull request targeting `main` is marked ready for review
  - skips draft pull requests
  - requires the pull request description to be present
  - requires the PR to have at least one label
- `release-pr.yml`
  - runs manually through `workflow_dispatch`
  - requires the workflow to be started from `main`
  - bumps the root `VERSION` file by `patch`, `minor`, or `major`
  - updates `sample.env` so `SAGE_IMAGE_TAG` matches the release version
  - creates a `release/v<VERSION>` branch with those file changes
  - opens a draft release PR titled `v<VERSION>`
  - fills the PR body with the generated GitHub release notes
  - adds a draft-only compare link as a PR comment for reviewer use
  - applies the combined labels from the merged PRs since the previous release
- `publish.yml`
  - runs on pushes to `main` that change `VERSION` or `sample.env`
  - validates that the merged pull request associated with the pushed commit is titled `v<VERSION>`
  - builds and publishes a GHCR image from that exact version
  - publishes only the exact `VERSION` image tag
  - creates a GitHub release with generated release notes configured by `.github/release.yml`
  - uses the highest existing `vMAJOR.MINOR.PATCH` tag lower than the current release as the previous release-note boundary when available
  - generates release notes from the changes on `main` before the release PR merge commit so the release PR itself is not included as a changelog entry

## Documentation Notes

This documentation set is based on the current repository scan. If runtime behavior changes, update these docs together with the corresponding code.
