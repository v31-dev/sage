# Architecture

## System Summary

Sage is a manager-and-workers platform designed for Docker workloads connected through Tailscale.

- The manager is the control plane.
- Workers run application containers and edge components.
- Cloudflare is required (env vars): DNS records, the tunnel for public apps, and the DNS-01 challenge for Sage's certificates.
- Traefik provides ingress on workers. The manager issues two certificates in-process (ACME DNS-01): its own for `<TS_NAME>.core.<domain>`, whose key never leaves it, and the workers' `*.int.<domain>` wildcard, synced to every worker.
- Vector runs on workers and forwards their container logs to the manager; the manager captures its own logs in-process.
- Telegraf runs on workers and pushes the host's and the containers' metrics and the containers' state to the manager every 10 s; the manager collects its own container metrics in-process.

## Manager Runtime

The container entrypoint (`app/entrypoint.sh`) first brings up the manager's own tailnet
node — `tailscaled` in userspace networking mode, state under `/app/data/tailscale` — and
then starts the manager process. Tailnet connections to the node's IP are delivered to
the same port on `127.0.0.1` inside the container, so `:80` and `:9001` need no published
ports — and anything listening there is reachable from the tailnet wherever the tailnet
policy allows.

In userspace mode the container has no route into the tailnet, so outbound traffic goes
through `tailscaled`: SSH to workers uses asyncssh's `proxy_command` with
`tailscale nc <worker> 22`. For HTTP the workers reach the manager (the reports and logs on
`:9001`), not the other way round.

The manager process starts these concurrent services from `app/main.py`:

1. Main FastAPI API and UI on port `80`, plain HTTP — it is reached over the tailnet,
   which WireGuard already encrypts
2. Ingestion FastAPI API on port `9001` (receives workers' Telegraf reports and Vector log shipments)
3. APScheduler (cron/interval) scheduler

Startup is service-driven:

- `Manager()` initializes the platform services.
- Before serving, startup sets up the domain (`Manager.setup_domain`): connect to Cloudflare (zone, tunnel, the manager's record) and get the manager's certificate, retrying a failure that clears by itself (a network error, HTTP 5xx or 429) after 1, 4 and 16 minutes and then exiting; any other failure, such as a rejected token, and a rate limit exit at once, and a still-valid certificate lets startup continue. Other startup work that touches the network (release check, the workers' certificate, S3 backup discovery, settings checks, re-checking online workers against their stamps) runs as queued tasks once the servers are up. S3 and notifications are optional.
- If service initialization fails, the process exits.

## Worker Runtime

Workers are bootstrapped remotely over Tailscale SSH (asyncssh): the manager runs remote commands and copies files via SFTP from `app/templates/worker/` to `/opt/sage` on each worker, including:

- `docker-compose.yml`
- worker `.env`
- Traefik config
- Vector config
- Telegraf config

The worker stack currently includes:

- `traefik`
- `vector`
- `telegraf`
- `cloudflared`, connecting the worker to the manager's tunnel

`vector` and `telegraf` run with a 128 MiB memory limit, swap included: their executables are large, and the kernel keeps the pages it has read in the container's memory, which on a small worker the applications could use.

Applications themselves are also deployed onto workers as Docker workloads.

## Traffic Model

Current traffic and naming patterns are driven by Cloudflare, Traefik, and Tailscale:

- The manager UI and API are only at `https://<TS_NAME>.core.<domain>`: a DNS-only record pointing at its tailnet IP, served with the manager's own certificate on `:443`. Plain HTTP (`:80`) only redirects there, and so does `:443` for any other hostname (the tailnet name or IP); in dev the dev Traefik enforces the same by matching the hostname. `.core` is a namespace apps never use, and workers hold no certificate for it.
- Public traffic uses Cloudflare-managed domains and tunnels.
- Internal routing uses Tailscale IPs and `*.int.<domain>` style records.

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
- `Task` (log of finished queue operations)

Its sensitive fields are encrypted with `ENCRYPTION_KEY` (Fernet, `utils/db.py`). The
same key encrypts every backup, platform and volume alike, in one format (`utils/archive.py`
on the manager, `backup.sh` and `restore.sh` on workers): `openssl enc -aes-256-cbc
-pbkdf2`, then a 32-byte HMAC-SHA256 trailer that is checked before anything is decrypted.
Files stream through the `openssl` in the image, so memory stays flat however large a
backup is. The key can't change after the first boot: the settings rows are written on the first boot and decrypted on every
later one, so a different key stops startup (`main.py`).

Relationships are centered on:

- project -> applications
- application -> containers, domains, volumes, backups
- worker -> containers

## Metrics And Logs Storage

Metrics and logs are intentionally sharded:

- Metrics: one SQLite database per hostname under `/app/data/metrics/`
- Logs: one SQLite database per container under `/app/data/logs/`

Container log search uses SQLite FTS5. Worker metric shards are populated from
the workers' Telegraf reports (per-minute peaks of 10 s readings); the manager's own shard is populated in-process from cgroup v2 and the data
volume (its single sage container, so no host load average). Worker container logs
arrive via each worker's Vector (the `:9001` ingestion API); the manager's own logs
are written to the `sage` shard in-process by a logging handler (no Vector sidecar).

Log storage is bounded at both ends. On a worker, every container (the four of the stack and each
application) keeps its Docker `json-file` logs in at most five files of 10 MB; the Compose templates set
it, so an application container gets it when it is next deployed. On the manager, a container's shard is
limited to 256 MiB of data and all shards together to 2 GiB or 10% of the data volume, whichever is smaller
(`LOG_SHARD_MAX_BYTES`, `LOG_TOTAL_MAX_BYTES`, `LOG_TOTAL_MAX_DISK_SHARE` in `config.py`), next to the 14-day
retention. Over a limit, the oldest lines go first (by timestamp), down to 90% of it and never below a
container's newest 1,000 lines. The check runs after writes (at most every 30 s) and in the daily cleanup.

## Pushed Updates

The UI keeps one Socket.IO connection to the manager (`/api/socket.io`) and follows topics over it instead of polling: the manager's status (`info`), the tasks (`tasks`, and the finished ones as `task-history`), the backup and discovery status, the release and the notifications. A page loads a topic from its REST route, and the manager pushes the whole state of a state topic each time it changes and each new row of a feed (a notification, a finished task) as it is stored; the page loads again each time the socket connects, so it makes up for what it missed. Socket.IO reconnects and falls back from WebSocket to long-polling by itself, a heartbeat every 10 s shows in an indicator at the top left of the UI, and a connection from another origin is refused. See [backend.md](backend.md) and [frontend.md](frontend.md).

## Scheduler And Operation Queue

APScheduler is a pure cron/interval trigger: each scheduled coroutine only calls `Manager().add_task(...)` to enqueue work and owns no execution or concurrency logic. Recurring triggers cover:

- worker sync
- application status sync
- scheduled volume backup dispatch
- Traefik domain config sync
- metrics collection
- platform backup scheduling
- cleanup
- certificate renewal

All execution and mutual exclusion live in an in-memory operation queue on the `Manager` singleton (`app/utils/queue.py`). Routes (deploy, stop, delete, backup, restore, worker removal and reboot) and the cron triggers above enqueue through the same `add_task` path; a single one-second dispatcher starts each pending task whose scope is free. See [backend.md](backend.md) for the queue model and execution semantics.

## Service Pattern

Services under `app/services/` are global singletons using a shared base pattern:

- one instance per service class
- thread-safe initialization
- per-service reentrant lock for mutable operations

This pattern is central to how the backend is organized.
