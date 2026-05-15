# Architecture

## System Summary

Sage is a manager-and-workers platform designed for Docker workloads connected through Tailscale.

- The manager is the control plane.
- Workers run application containers and edge components.
- Cloudflare provides public DNS and tunnel integration.
- Traefik provides ingress and certificate handling.
- Vector forwards logs to the manager.
- Glances provides metrics endpoints that the manager polls.

## Manager Runtime

The manager process starts three concurrent services from `app/main.py`:

1. Main FastAPI API on port `9000`
2. Vector ingestion FastAPI API on port `9001`
3. Rocketry async scheduler

Startup is service-driven:

- `Manager()` initializes the platform services.
- `Manager.async_init()` performs async startup work, such as backup discovery.
- If service initialization fails, the process exits.

## Worker Runtime

Workers are bootstrapped remotely over Tailscale SSH and rsync. The manager syncs files from `app/templates/worker/` to `/opt/sage` on each worker, including:

- `docker-compose.yml`
- worker `.env`
- Traefik config
- Vector config

The worker stack currently includes:

- `cloudflared`
- `traefik`
- `vector`
- `glances`

Applications themselves are also deployed onto workers as Docker workloads.

## Traffic Model

Current traffic and naming patterns are driven by Cloudflare, Traefik, and Tailscale:

- Public traffic uses Cloudflare-managed domains and tunnels.
- Internal routing uses Tailscale IPs and `*.int.<domain>` style records.
- Manager UI and API are exposed under the `*.core.<domain>` naming convention.

The manager keeps DNS and Traefik state in sync as workers and application domains change.

## Main Data Model

The main manager database is SQLite with WAL mode and lives at `/app/data/data.db`.

Core entities:

- `Setting`
- `Worker`
- `Project`
- `Application`
- `Container`
- `Domain`
- `Volume`
- `Event`
- `Notification`
- `Backup`

Relationships are centered on:

- project -> applications
- application -> containers, domains, volumes, backups
- worker -> containers

## Metrics And Logs Storage

Metrics and logs are intentionally sharded:

- Metrics: one SQLite database per hostname under `/app/data/metrics/metrics/`
- Logs: one SQLite database per container under `/app/data/metrics/logs/`

Container log search uses SQLite FTS5.

## Scheduler Responsibilities

The Rocketry scheduler handles recurring control-plane work such as:

- worker discovery and sync
- application status sync
- scheduled volume backup dispatch
- Traefik domain config sync
- metrics collection
- platform backup scheduling
- cleanup
- certificate sync

It also runs on-demand multilaunch tasks for deployment, stop, delete, and backup flows.

## Service Pattern

Services under `app/services/` are global singletons using a shared base pattern:

- one instance per service class
- thread-safe initialization
- per-service reentrant lock for mutable operations

This pattern is central to how the backend is organized.
