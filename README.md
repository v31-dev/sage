# sage

Sage is a lightweight single-user micro-PaaS built around Tailscale, Cloudflare, FastAPI, Rocketry, SQLite, and a Vue 3 UI. It manages a single manager node plus one or more worker nodes, deploys Docker-based applications, syncs ingress and DNS, collects metrics and logs, and supports S3-backed backups.

This README is a project entrypoint based on the current workspace state. Detailed notes live in [`docs/`](docs/).

## Table of Contents

- [Overview](#overview)
- [Architecture At A Glance](#architecture-at-a-glance)
- [Core Features](#core-features)
- [Repository Layout](#repository-layout)
- [Configuration](#configuration)
- [Getting Started](#getting-started)
- [Development](#development)
- [Deployment](#deployment)
- [More Docs](#more-docs)
- [AI Agent Context](#ai-agent-context)

## Overview

Sage runs as a manager service that coordinates workers over a Tailscale network.

- The manager exposes the main API and UI.
- Workers run application containers plus supporting edge components.
- Cloudflare DNS and tunnels are used for public routing.
- Traefik handles ingress and certificates.
- Vector forwards logs to the manager.
- Glances provides system and container metrics.

The current codebase is optimized for a small manager footprint and simple operational patterns instead of heavy orchestration.

## Architecture At A Glance

- `app/main.py` starts three concurrent services in one process:
  - Main FastAPI API on port `9000`
  - Vector ingestion FastAPI API on port `9001`
  - Rocketry scheduler
- `app/services/manager.py` is the orchestration layer for workers, deployments, backups, restore flows, notifications, and sync tasks.
- `app/services/db/` stores manager state in SQLite with WAL mode.
- `app/services/metrics.py` stores metrics in per-host SQLite files and logs in per-container SQLite files with FTS5 search.
- `ui/` is a Vue 3 + TypeScript SPA for projects, applications, workers, logs, metrics, backups, and settings.

For more detail, see [docs/architecture.md](docs/architecture.md).

## Core Features

- Manage projects, applications, containers, domains, volumes, workers, settings, backups, and notifications
- Deploy applications from Docker images or public Git repositories
- Stop and delete deployed workloads
- Route public and internal application traffic through generated Traefik config via a mesh on all workers
- Discover workers over Tailscale and bootstrap their runtime files
- Collect worker and container metrics
- Ingest and search container logs
- Create platform backups and application volume backups with S3 storage
- Restore platform data and application volumes

## Repository Layout

| Path | Purpose |
| --- | --- |
| `app/` | Python backend, scheduler, services, templates |
| `app/routes/` | FastAPI route handlers |
| `app/services/` | Singleton services for orchestration, infra, metrics, settings, storage |
| `app/services/db/` | Peewee models and database bootstrap |
| `app/templates/` | Manager and worker config templates |
| `app/utils/` | Logging, API helpers, encrypted DB fields, shared utilities |
| `ui/` | Vue 3 frontend |
| `docs/` | Project documentation |
| `setup/dev/` | Local machine setup scripts |
| `.github/workflows/` | CI and image publish workflows |
| `docker-compose.yml` | Main runtime compose stack |
| `docker-compose.override.yml` | Development overrides |

## Configuration

Environment variables are documented in [`sample.env`](sample.env). Key inputs include:

- `ENCRYPTION_KEY` for encrypted database fields and backup-related secrets
- `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `DOMAIN`, `ADMIN_EMAIL`
- `HOSTNAME`, `TS_IP`, `SAGE_HOME`
- Optional S3 settings for backups
- Optional Discord webhook for notifications

Settings are also persisted through the database via the settings service.

## Getting Started

Production-style stack:

```bash
docker compose up -d
```

Development stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

Local environment bootstrapping helpers:

```bash
./setup/dev/ubuntu.sh
./setup/dev/env.sh
```

The backend service expects access to:

- Docker socket
- Tailscale socket
- writable data directories under `SAGE_HOME`

## Development

Backend commands run from `app/`:

```bash
ruff check
autopep8 --in-place --recursive .
isort .
python main.py
```

Frontend commands run from `ui/`:

```bash
npm run format:check
npm run format
npm run build
npm run dev
```

More operational notes live in [docs/development.md](docs/development.md).

## Deployment

The GitHub workflow in `.github/workflows/publish.yml` publishes the Docker image to GitHub Container Registry when changes land on `main` for:

- `app/**`
- `ui/**`
- `VERSION`
- `Dockerfile`
- `.github/workflows/publish.yml`

The workflow reads the root `VERSION` file, builds the top-level Dockerfile, pushes `ghcr.io/v31-dev/sage`, and creates a GitHub release named from that version.

Published image tags:

- `latest`
- the exact `VERSION` value, such as `0.1.1` or `0.2`
- the major/minor tag, such as `0.1`
- the major tag, such as `0`

Release tags are created with a leading `v`, such as `v0.1.1` or `v0.2`.

To deploy a release:

1. Update `VERSION`.
2. Merge the change to `main`.
3. Wait for the publish workflow to complete.
4. Pull the desired image tag from GHCR.

```bash
docker pull ghcr.io/v31-dev/sage:latest
docker pull ghcr.io/v31-dev/sage:0.1
```

The PR check in `.github/workflows/pr-check.yml` only runs when `VERSION` changes and verifies that the pull request version differs from `main`.

### Versioning Notes

Strict Semantic Versioning uses `MAJOR.MINOR.PATCH`, for example `1.2.3`. If this project uses only `MAJOR.MINOR`, treat that as project release versioning rather than strict SemVer.

GitHub and Git do not automatically resolve partial version tags. A tag like `v1` only points to the latest `v1.x` release if a workflow explicitly creates or moves the `v1` tag. This repo's publish workflow already publishes Docker tags for the exact version, major/minor, major, and `latest`; the GitHub release tag itself is only the exact `v<VERSION>` value.

## More Docs

- [docs/architecture.md](docs/architecture.md)
- [docs/backend.md](docs/backend.md)
- [docs/frontend.md](docs/frontend.md)
- [docs/development.md](docs/development.md)

## AI Agent Context

Future task-oriented agents should start with [`AGENTS.md`](AGENTS.md). That file gives agents the project context, working rules, and pointers to the focused docs they should read before changing code.
