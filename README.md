# sage

Sage is a lightweight single-user micro-PaaS for running and managing Docker-based applications across a small set of machines. It uses Tailscale for node connectivity, Cloudflare for domains, DNS and public apps, Traefik for ingress, and a web UI for day-to-day operations.

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
- Cloudflare provides the domain: DNS, the tunnel for public apps, and the DNS-01
  validation for Let's Encrypt certificates.
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
  no tailnet identity yet (first boot, or after its state is lost). If the node can't join
  the tailnet with it (an invalid or expired key), the container exits at once with a
  `[CRITICAL]` line.
- `TS_NAME` — the manager's tailnet machine name, also its name inside Sage.
- `TS_WORKER_TAG` — the Tailscale tag that marks worker machines (with or without the
  `tag:` prefix).
- `DOMAIN` — a domain whose DNS is on Cloudflare (a zone in the account below). Apps get
  addresses under it, and the UI is served only at `https://<TS_NAME>.core.<DOMAIN>`.
- `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` — the account holding the zone, and a
  token with Account › Cloudflare Tunnel › Edit, Zone › DNS › Edit and Zone › Zone › Read
  for the zone.
- `ENCRYPTION_KEY` — encrypts secrets in the database, platform backups and volume
  backups. Generate one with `openssl rand -base64 32 | tr '+/' '-_'` and keep it with
  your S3 credentials: backups can't be restored without it. It can't change after the
  first boot: Sage refuses to start with a different key. To use another key, wipe the
  volume and start again. Restoring a backup needs the key it was made with, so recovering
  onto a new volume means starting with the original key.
- A persistent volume at `/app/data`.
- Exactly one running instance.
- A stop grace period of a few minutes. On a stop signal Sage starts no new work and lets
  running tasks (a deploy, a backup) finish before it exits. Docker's default is 10 seconds
  before it kills the container.

`.env`:

```bash
TS_AUTH_KEY=tskey-auth-...
TS_NAME=sage
TS_WORKER_TAG=worker
DOMAIN=example.com
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...
ENCRYPTION_KEY=...
```

Leave the values unquoted: `docker run --env-file` keeps quotes as part of the value.

With `docker run`:

```bash
docker run -d --name sage --restart unless-stopped --stop-timeout 300 \
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
    stop_grace_period: 5m
    env_file: .env
    volumes:
      - sage-data:/app/data

volumes:
  sage-data:
```

`latest` follows the newest release. Pin a release tag (`ghcr.io/v31-dev/sage:<version>`,
see the GitHub releases) to control when you upgrade.

On start, before serving anything, Sage sets up Cloudflare (the tunnel and the manager's
DNS record) and gets the manager's certificate from Let's Encrypt. A first start can take
a few minutes. If that keeps failing (a wrong token, Cloudflare unreachable), Sage retries
for about 20 minutes and then exits, and its log says why. With a certificate from an
earlier start, it serves even while Cloudflare is unreachable.

`unless-stopped` restarts Sage after a crash and after a host reboot, without limit. A
start that keeps failing then loops, which Let's Encrypt tolerates: failed validations are
capped at 5 per hour, Sage stops immediately once it is rate limited, and Let's Encrypt
doesn't ban addresses. If you'd rather it give up, `restart: on-failure:5` (`--restart
on-failure:5`) stops after 5 failed starts, but Docker doesn't restart `on-failure`
containers after a host reboot, so you then start Sage by hand.

### Tailnet

Enable MagicDNS: the manager reaches workers by name. The UI's address,
`<TS_NAME>.core.<DOMAIN>`, is a Cloudflare DNS record pointing at the manager's tailnet
IP, so it only works from the tailnet. Plain `http://<TS_NAME>` redirects to it.

**Sage has no login or access control of its own.** Anyone who can reach the manager over
the tailnet can use the UI and API, including reading the stored S3 keys and Discord
webhook. The tailnet policy is the only gate: restrict who can reach the manager,
and never expose it outside the tailnet (no published ports, no public URL).

In the tailnet policy, give the manager and the workers their own tags (the examples use
`tag:manager` and `tag:worker`) and allow the traffic Sage uses. Anything not allowed is
unreachable; tailnet traffic reaches every port the manager container listens on.

```jsonc
{
  "tagOwners": {
    "tag:manager": ["autogroup:admin"],
    "tag:worker":  ["autogroup:admin"]
  },
  "acls": [
    // Your devices → the manager UI (443; 80 only redirects to it)
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:manager:80,443"]},
    // Workers → the manager's log ingestion
    {"action": "accept", "src": ["tag:worker"], "dst": ["tag:manager:9001"]},
    // Manager → workers: SSH (Tailscale SSH) and Glances metrics
    {"action": "accept", "src": ["tag:manager"], "dst": ["tag:worker:22,61208"]},
    // Workers ↔ workers: the Traefik mesh
    {"action": "accept", "src": ["tag:worker"], "dst": ["tag:worker:9002,9003"]},
    // Internal app domains (*.int) served by workers
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

Set the same tag in `TS_WORKER_TAG`. The manager discovers machines carrying it and sets
them up over SSH.

### First steps

Once Sage is up, the UI is at `https://<TS_NAME>.core.<DOMAIN>`. S3 (backups) and
notifications are optional and set in the UI.

Sage lists the backups already in the S3 bucket and path when you save the S3 settings,
at startup and from Discover backups on the Backups page. To recover onto a new volume,
start with the original `ENCRYPTION_KEY`, set S3, then restore a platform backup; volume
backups are listed once their application is back. Each backup can also be downloaded
from the Backups page (platform) or a volume's Restore dialog (volume): a link that works
for a minute, straight from S3. The files are encrypted; Settings → S3 shows how to open one.

To change `TS_NAME`, `TS_WORKER_TAG` or the Cloudflare details, edit the env and restart:
Sage brings the workers in line on start. A new `TS_NAME` renames the manager's tailnet
machine. Records and tunnels of a previous domain, account or name stay in Cloudflare
until you delete them.

Sage doesn't upgrade itself. The version in the footer opens Settings → Upgrade, with the
release notes and the steps: take a backup, pause Sage and wait until it is safe to stop,
then run the newer image against the same volume. Sage always starts unpaused. The backup
comes first because a paused Sage refuses changes and starts no tasks.

The repo's [`docker-compose.yml`](docker-compose.yml) and [`sample.env`](sample.env) are
for local development; see [docs/development.md](docs/development.md).

## More Docs

- [docs/architecture.md](docs/architecture.md)
- [docs/backend.md](docs/backend.md)
- [docs/frontend.md](docs/frontend.md)
- [docs/development.md](docs/development.md)
- [docs/profiling.md](docs/profiling.md)
