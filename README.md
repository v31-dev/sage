# sage

Sage is a lightweight single-user micro-PaaS for running and managing Docker-based applications across a small set of machines. It uses Tailscale for node connectivity, Cloudflare for DNS and tunnels, Traefik for ingress, and a web UI for day-to-day operations.

Detailed technical and development-oriented material lives in [`docs/`](docs/).

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Usage](#usage)
- [More Docs](#more-docs)

## Overview

Sage is built around a manager node that coordinates one or more worker nodes over a private Tailscale network.

- The manager provides the API and web UI.
- Workers run application containers and supporting edge services.
- Cloudflare is used for public DNS and tunnel management.
- Traefik handles ingress and routing.
- Metrics and logs are collected centrally for visibility and troubleshooting.
- Backups can be stored with S3-compatible object storage.

Sage is designed for small, practical self-hosted deployments rather than large-cluster orchestration.

## Architecture

At a high level, Sage has three main layers:

- A manager service that stores platform state, coordinates deployments, and exposes the UI and API
- Worker nodes that run applications, ingress, and supporting runtime services
- Shared platform integrations for networking, DNS, metrics, logs, and backups

The manager-to-workers model over Tailscale is the core operating pattern.

For deeper architecture details, see [docs/architecture.md](docs/architecture.md).

## Features

- Manage projects, applications, containers, domains, volumes, workers, settings, backups, and notifications
- Deploy applications from Docker images or public Git repositories
- Stop and delete deployed workloads
- Route public and internal traffic through generated Traefik configuration
- Discover and bootstrap workers over Tailscale
- Collect worker and container metrics
- Ingest and search container logs
- Create and restore platform and application backups

## Usage

Sage is intended to be run with the provided `docker-compose.yml` and `sample.env`.

1. Copy [`sample.env`](sample.env) into your environment file and fill in the required values for your installation.
2. Set `SAGE_IMAGE_TAG` to the exact Sage release version you want to run.
3. Start the stack with Docker Compose:

```bash
docker compose up -d
```

The main runtime uses:

- [`docker-compose.yml`](docker-compose.yml) for the production-style stack
- [`sample.env`](sample.env) as the reference for required configuration values

If you need implementation details, development workflow notes, or release automation details, use the docs below instead of this README.

## More Docs

- [docs/architecture.md](docs/architecture.md)
- [docs/backend.md](docs/backend.md)
- [docs/frontend.md](docs/frontend.md)
- [docs/development.md](docs/development.md)
