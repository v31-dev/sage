# sage

Sage is a lightweight single-user micro-PaaS for running and managing Docker-based applications across a small set of machines. It uses Tailscale for node connectivity, Traefik for ingress, optionally Cloudflare for domains and public apps, and a web UI for day-to-day operations.

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
- Cloudflare (optional) provides app domains: DNS, the tunnel for public apps, and TLS
  certificates.
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
- Deploy applications from Docker images or Git repositories (private repos via a `GIT_AUTH_TOKEN` build secret)
- Stop and delete deployed workloads
- Route public and internal traffic through generated Traefik configuration
- Discover and bootstrap workers over Tailscale
- Collect worker and container metrics
- Ingest and search container logs
- Create and restore platform and application backups

## Usage

### Manager

The manager is a single container image, `ghcr.io/v31-dev/sage`. It runs anywhere that
runs a container with a persistent volume. It joins your tailnet as its own node, so it
needs no published ports, public URL, host Tailscale or TUN device.

It needs:

- `TS_AUTH_KEY` — a Tailscale auth key for the manager's node. Create it **tagged** (the
  tag becomes the manager's tag) and **not ephemeral**. It is only used while the node has
  no tailnet identity yet (first boot, or after its state is lost).
- `TS_NAME` — the manager's tailnet machine name, also its name inside Sage.
- A persistent volume at `/app/data`.
- Exactly one running instance.

`.env`:

```bash
TS_AUTH_KEY=tskey-auth-...
TS_NAME=sage
```

Leave the values unquoted: `docker run --env-file` keeps quotes as part of the value.

With `docker run`:

```bash
docker run -d --name sage --restart unless-stopped \
  --env-file .env \
  -v sage-data:/app/data \
  ghcr.io/v31-dev/sage:latest
```

With Docker Compose:

```yaml
services:
  sage:
    image: ghcr.io/v31-dev/sage:latest
    restart: unless-stopped
    env_file: .env
    volumes:
      - sage-data:/app/data

volumes:
  sage-data:
```

`latest` follows the newest release. Pin a release tag (`ghcr.io/v31-dev/sage:<version>`,
see the GitHub releases) to control when you upgrade.

### Tailnet

Enable MagicDNS: the UI is reached as `http://<TS_NAME>`, and the manager reaches workers
by name.

In the tailnet policy, give the manager and the workers their own tags (the examples use
`tag:manager` and `tag:worker`) and allow the traffic Sage uses. Anything not allowed is
unreachable, which is what keeps the manager private: tailnet traffic reaches every port
the manager container listens on.

```jsonc
{
  "tagOwners": {
    "tag:manager": ["autogroup:admin"],
    "tag:worker":  ["autogroup:admin"]
  },
  "acls": [
    // Your devices → the manager UI
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:manager:80"]},
    // Workers → the manager's log ingestion
    {"action": "accept", "src": ["tag:worker"], "dst": ["tag:manager:9001"]},
    // Manager → workers: SSH (Tailscale SSH) and Glances metrics
    {"action": "accept", "src": ["tag:manager"], "dst": ["tag:worker:22,61208"]},
    // Workers ↔ workers: the Traefik mesh
    {"action": "accept", "src": ["tag:worker"], "dst": ["tag:worker:9002,9003"]},
    // With a domain configured: internal app domains (*.int) served by workers
    {"action": "accept", "src": ["autogroup:member", "tag:worker"], "dst": ["tag:worker:443,8443"]}
  ],
  "ssh": [
    // Tagged sources must use "accept" (not "check")
    {"action": "accept", "src": ["tag:manager"], "dst": ["tag:worker"], "users": ["root"]}
  ]
}
```

### Workers

A worker is a Linux machine with Docker (including the Compose plugin) that joins the
tailnet with the worker tag and Tailscale SSH enabled:

```bash
tailscale up --ssh --auth-key=tskey-auth-...   # a key tagged tag:worker
```

Set the same tag in the UI (Settings → Tailscale). The manager discovers machines
carrying it and sets them up over SSH. No workers are discovered until the tag is set.

### First steps

Once the node is up, the UI is at `http://<TS_NAME>`. Everything else is configured in the
UI: start with the worker tag. Cloudflare (app domains, public apps), S3 (backups) and
notifications are optional.

Changing `TS_NAME` later renames the manager's tailnet machine on the next start. To
upgrade, run the newer image against the same volume.

The repo's [`docker-compose.yml`](docker-compose.yml) and [`sample.env`](sample.env) are
for local development; see [docs/development.md](docs/development.md).

## More Docs

- [docs/architecture.md](docs/architecture.md)
- [docs/backend.md](docs/backend.md)
- [docs/frontend.md](docs/frontend.md)
- [docs/development.md](docs/development.md)
- [docs/profiling.md](docs/profiling.md)
