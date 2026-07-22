# Architecture

## System Summary

Sage is a manager-and-workers platform designed for Docker workloads connected through Tailscale.

- The manager is the control plane.
- Workers run application containers and edge components.
- Cloudflare provides public DNS and tunnel integration.
- Traefik provides ingress on workers; the manager issues and owns the wildcard TLS certificate in-process (ACME DNS-01) and terminates its own `:443`.
- Vector runs on workers and forwards their container logs to the manager; the manager captures its own logs in-process.
- Glances runs on workers and exposes metrics endpoints the manager polls; the manager collects its own container metrics in-process.

## Manager Runtime

The manager process starts these concurrent services from `app/main.py`:

1. Main FastAPI API on port `9000`
2. Log/metric ingestion FastAPI API on port `9001` (receives workers' Vector log shipments)
3. TLS server on port `443` (Tailscale-only) serving the app under `sage.core.<domain>` with the wildcard cert
4. APScheduler (cron/interval) scheduler

Startup is service-driven:

- `Manager()` initializes the platform services.
- `Manager.async_init()` performs async startup work, such as backup discovery.
- The wildcard cert is provisioned (issued if missing/expiring) before `:443` binds; its `SSLContext` is handed to `Certs` so renewal swaps the cert in place with no restart.
- If service initialization fails, the process exits.

## Worker Runtime

Workers are bootstrapped remotely over Tailscale SSH (asyncssh): the manager runs remote commands and copies files via SFTP from `app/templates/worker/` to `/opt/sage` on each worker, including:

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

### Domain Types

A `Domain` has a `type` that selects how Traefik routes it:

- `public` — HTTP(S) via the Cloudflare tunnel on `<sub>.<domain>`.
- `internal` — HTTP(S) over the Tailscale mesh on `<sub>.int.<domain>`. The edge
  Traefik marks the request with `X-Mesh-Hops` and load-balances to the `mesh`
  entrypoint (`:9002`) of each backing worker, which forwards to the local
  container. WebSockets ride this path unchanged (HTTP Upgrade).
- `tcp` — raw TCP backing service (redis/postgres/...) over the mesh on
  `<sub>.int.<domain>:8443`. TCP has no Host header, so routing is by **TLS SNI** on
  a single shared client port (`tcps` = `:8443`). Clients connect with TLS (e.g.
  `rediss://`, libpq `sslmode=require`); Traefik handles Postgres STARTTLS
  automatically (Traefik 3.x), so no `sslnegotiation=direct` or sidecar is needed.
  The edge router is **passthrough** (so the SNI survives to the mesh hop) and
  load-balances across every active backing worker, exactly like the internal/public
  routers. The final hop on `meshtcp` (`:9003`, the TCP analog of `mesh:9002`)
  **terminates** TLS with the `*.int` wildcard from the worker's synced PEM (loaded
  via the Traefik file provider) and forwards plaintext to the local stock container — so the cert never leaves
  Traefik and backends need no TLS config. The separate `meshtcp` entrypoint stands
  in for the `X-Mesh-Hops` header that TCP cannot carry. `tcp` is Tailscale-only (no
  public variant) and has no `x-tag` pool variant, but multi-container load balancing
  works the same as other domain types.

**`tcp` clients must send SNI.** Routing is purely by SNI, so the client must connect
with TLS and present SNI = `<sub>.int.<domain>`. Without it Traefik cannot select the
route and serves its default self-signed cert, so the client fails with a
"self-signed certificate" error. Most clients derive the SNI from the connection host
automatically (`rediss://`, libpq `sslmode=require`, `openssl s_client`), but some do
not — **node-redis** is one: pass the host explicitly as the TLS `servername`:

```js
const url = process.env.REDIS_URL // rediss://<sub>.int.<domain>:8443
createClient({ url, socket: { servername: new URL(url).hostname } })
```

To see what a worker presents for a given SNI (real wildcard vs. the default cert):

```bash
# with -servername -> CN=<domain> (Let's Encrypt); with -noservername -> TRAEFIK DEFAULT CERT
openssl s_client -connect <sub>.int.<domain>:8443 -servername <sub>.int.<domain> \
  </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer
```

For same-node, same-protocol access between containers, skip the mesh entirely and
use the Docker DNS name (the container's `qualified_name`) on `sage_default`.

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

- Metrics: one SQLite database per hostname under `/app/data/metrics/`
- Logs: one SQLite database per container under `/app/data/logs/`

Container log search uses SQLite FTS5. Worker metric shards are populated from
Glances; the manager's own shard is populated in-process from cgroup v2 and the data
volume (its single sage container, so no host load average). Worker container logs
arrive via each worker's Vector (the `:9001` ingestion API); the manager's own logs
are written to the `sage` shard in-process by a logging handler (no Vector sidecar).

## Scheduler And Operation Queue

APScheduler is a pure cron/interval trigger: each scheduled coroutine only calls `Manager().add_task(...)` to enqueue work and owns no execution or concurrency logic. Recurring triggers cover:

- worker discovery and sync
- application status sync
- scheduled volume backup dispatch
- Traefik domain config sync
- metrics collection
- platform backup scheduling
- cleanup
- certificate renewal

All execution and mutual exclusion live in an in-memory operation queue on the `Manager` singleton (`app/utils/queue.py`). Routes (deploy, stop, delete, backup, restore, worker removal, restart) and the cron triggers above enqueue through the same `add_task` path; a single one-second dispatcher starts each pending task whose scope is free. See [backend.md](backend.md) for the queue model and execution semantics.

## Service Pattern

Services under `app/services/` are global singletons using a shared base pattern:

- one instance per service class
- thread-safe initialization
- per-service reentrant lock for mutable operations

This pattern is central to how the backend is organized.
