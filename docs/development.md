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
- optional notification settings
- optional S3 backup settings

## Running The Stack

Production-style compose:

```bash
docker compose up -d
```

Development compose:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

The main compose stack runs:

- `sage`
- `traefik`
- `vector`
- `glances`

The development override also adds a separate `ui` service and backend debug configuration.

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
2. the Python app image that serves the built frontend from `/app/static`

## CI And Release Notes

Current GitHub workflows:

- `pr-check.yml`
  - validates that `VERSION` changed in a pull request targeting `main`
- `publish.yml`
  - builds and publishes a GHCR image on pushes to `main`
  - tags releases based on `VERSION`

## Documentation Notes

This documentation set is based on the current repository scan. If runtime behavior changes, update these docs together with the corresponding code.
