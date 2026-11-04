# Architecture

## System Summary

Sage is a manager-and-workers platform designed for Docker workloads connected through Tailscale.

- The manager is the control plane.
- Workers run application containers and edge components.
- Cloudflare is required (env vars): DNS records, a tunnel per worker for public apps, and the DNS-01 challenge for Sage's certificates.
- Traefik provides ingress on workers, each routing only to its own containers. The manager issues two certificates in-process (ACME DNS-01): its own for `<TS_NAME>.core.<domain>`, whose key never leaves it, and the workers', covering `*.int.<domain>` and each worker's `*.<subdomain>.int.<domain>`, synced to every worker.
- Vector runs on workers and forwards Traefik's and the applications' logs to the manager, a batch at least every 10 s; the manager captures its tasks' logs in-process.
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
- `cloudflared`, running the worker's own tunnel
- `cloudflared-discovery`, a replica of the platform's tunnel, which every worker runs

`vector` and `telegraf` run with a 128 MiB memory limit, swap included: their executables are large, and the kernel keeps the pages it has read in the container's memory, which on a small worker the applications could use.

Applications themselves are also deployed onto workers as Docker workloads.

## Traffic Model

Current traffic and naming patterns are driven by Cloudflare, Traefik, and Tailscale:

- The manager UI and API are only at `https://<TS_NAME>.core.<domain>`: a DNS-only record pointing at its tailnet IP, served with the manager's own certificate on `:443`. Plain HTTP (`:80`) only redirects there, and so does `:443` for any other hostname (the tailnet name or IP); in dev the dev Traefik enforces the same by matching the hostname. `.core` is a namespace apps never use, and workers hold no certificate for it.
- A domain is a name linked to one container, and the worker that runs it routes it to that container with its own
  Traefik: there is no hop between workers. Domains are managed on the Domains page; an application's page lists the
  names linked to its containers.
- A worker has a **subdomain** (one DNS label, unique; random when none is given) that its internal names are under.
- Public names are proxied CNAMEs to the tunnel of the worker their container is on. Every worker's tunnel carries the
  same ingress (`*.<domain>` to Traefik, then 404), so the record alone decides the worker.
- Internal and TCP names are `<name>.<subdomain>.int.<domain>`, through one DNS-only `*.<subdomain>.int.<domain>` record
  per worker pointing at its tailnet IP. No record names an application, so application names stay out of public DNS.
- **Discovery** lists a service's names for clients that choose one: `discovery.<domain>?service=<service>` for public
  names (through the platform's tunnel, so any worker answers while the manager is down) and
  `discovery.int.<domain>?service=<service>` for internal and TCP ones (tailnet only, an A record per online worker).
  The answer is a header, `Sage-Endpoints: "<host>";pool="<pool>", …` (an RFC 9651 list), to HEAD, GET or OPTIONS,
  with `Cache-Control: private, max-age=300` and CORS for any origin; another method or an unknown service gets a 404.
  A client times `HEAD /ping` on each name to choose, which Traefik answers while the container is active (GET still
  reaches the application).

The manager keeps DNS and Traefik state in sync as workers and application domains change.

### Cloudflare Resources

Sage finds what it created in Cloudflare by name (a tunnel) or by comment (a DNS record). Tunnels belong to the
account, which several Sages may share, so a tunnel's name holds the domain. Records live in the domain's zone, so a
comment is `sage-<kind>`, with `-<hostname>` where several workers' records share a name. A comment is at most 100
characters on Cloudflare's Free plan, which has no record tags.

| Resource | Name or comment |
| --- | --- |
| The platform's tunnel, run by `cloudflared-discovery` on every worker: created at start, and never deleted with a worker | `sage-<domain>` |
| Each worker's own tunnel, run by its `cloudflared`: created when the worker is set up, deleted when it is removed | `sage-<domain>-<hostname>` |
| `discovery.<domain>`, a proxied CNAME to the platform's tunnel, its one record (any other with its comment is deleted), written by `sync_domain_records` | `sage-tunnel` |
| `<TS_NAME>.core.<domain>`, an A record to the manager's tailnet IP | `sage-manager` |
| `*.<subdomain>.int.<domain>`, an A record to the worker's tailnet IP: kept while the worker is offline, so its names work again as soon as it is back, and deleted when it is removed | `sage-worker-<hostname>` |
| `discovery.int.<domain>`, an A record to each online worker's tailnet IP, written by `sync_domain_records` | `sage-discovery-<hostname>` |
| Each public name linked to a container, a proxied CNAME to its worker's tunnel, written by `sync_domain_records` every 5 minutes and when routing or a worker's tunnel changes | `sage-domain` |
| `_acme-challenge.<name>`, a TXT record while a certificate is issued | `sage-acme-challenge` |

After a change of domain, account or `TS_NAME`, the previous domain's or name's resources stay in Cloudflare until
they are deleted by hand.

### Domain Types

A `Domain` is a name (one DNS label: lowercase letters, digits and dashes), a `type`, a port, the container it is
linked to, and optionally a service and a pool:

- `public` — `https://<name>.<domain>`, through the tunnel of its container's worker.
- `internal` — `https://<name>.<subdomain>.int.<domain>`, under its worker's subdomain, on the tailnet only (the
  `tailscale-only` allow list). WebSockets work unchanged (HTTP Upgrade).
- `tcp` — a raw TCP service (redis, postgres, ...) at `<name>.<subdomain>.int.<domain>:8443`, on the tailnet only. TCP
  has no Host header, so Traefik routes by **TLS SNI** on one shared port (`tcps`, `:8443`), ends TLS with the workers'
  certificate and forwards plain TCP to the container, so the container needs no TLS of its own. Clients connect with
  TLS (e.g. `rediss://`, libpq `sslmode=require`); Traefik handles Postgres STARTTLS (Traefik 3.x), so no
  `sslnegotiation=direct` or sidecar is needed.

Deleting a container unlinks its domains: they stay, listed on the Domains page, with no routing, and an internal one
keeps its worker, so it keeps its name. Linking it to a container on another worker moves an internal name there (its
subdomain in the name follows), so moving an application to another worker keeps its public names. A worker that has
internal domains, linked or not, can't be removed. A public name is one level under `<domain>`, because Cloudflare's
free edge certificate covers only first-level names, and it keeps its record while its container is stopped, so it
doesn't come and go with deploys; Traefik answers it while the container is active.

A public name is unique among the public domains, and an internal or TCP name once on each worker (they share
`<subdomain>.int.<domain>`). `core`, `int` and `discovery` are reserved public names.

Names that share a **service** are listed together at discovery, apart for each namespace (public, or internal and
TCP), and a **pool** is a group within a service. Clients that can't choose a name, such as browsers and webhook
senders, are given one name. An OAuth provider needs each name it redirects to registered.

**`tcp` clients must send SNI.** Routing is purely by SNI, so the client must connect
with TLS and present SNI = `<name>.<subdomain>.int.<domain>`. Without it Traefik cannot select the
route and serves its default self-signed cert, so the client fails with a
"self-signed certificate" error. Most clients derive the SNI from the connection host
automatically (`rediss://`, libpq `sslmode=require`, `openssl s_client`), but some do
not — **node-redis** is one: pass the host explicitly as the TLS `servername`:

```js
const url = process.env.REDIS_URL // rediss://<name>.<subdomain>.int.<domain>:8443
createClient({ url, socket: { servername: new URL(url).hostname } })
```

To see what a worker presents for a given SNI (the workers' certificate vs. the default one):

```bash
# with -servername -> issued by Let's Encrypt; with -noservername -> TRAEFIK DEFAULT CERT
openssl s_client -connect <name>.<subdomain>.int.<domain>:8443 -servername <name>.<subdomain>.int.<domain> \
  </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer
```

For access between containers on one worker, use the Docker DNS name (the container's `qualified_name`) on
`sage_default`.

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
- Logs: one SQLite database per application under `/app/data/logs/`, and beside them the requests (`requests.db`, Traefik's access lines in columns) and the task logs (`tasks.db`)

Application log search uses SQLite FTS5 with the `trigram` tokenizer, so text is found anywhere in a line. Worker metric shards are populated from
the workers' Telegraf reports (per-minute peaks of 10 s readings); the manager's own shard is populated in-process from cgroup v2 and the data
volume (its single sage container, so no host load average). Traefik's and the applications' logs
arrive via each worker's Vector (the `:9001` ingestion API); the manager keeps the lines of its queued
tasks, captured in-process by a logging handler (no Vector sidecar), as the task logs. Its other lines are
in stdout only, read with `docker logs`.

Log storage is bounded at both ends. On a worker, every container (the four of the stack and each
application) keeps its Docker `json-file` logs in at most five files of 10 MB; the Compose templates set
it, so an application container gets it when it is next deployed. On the manager, a container's shard is
limited to 256 MiB of data and all shards together to 2 GiB or 10% of the data volume, whichever is smaller
(`LOG_SHARD_MAX_BYTES`, `LOG_TOTAL_MAX_BYTES`, `LOG_TOTAL_MAX_DISK_SHARE` in `config.py`), next to the 14-day
retention. Over a limit, the oldest lines go first (by timestamp), down to 90% of it and never below a
container's newest 1,000 lines. The check runs after writes (at most every 30 s) and in the daily cleanup.
The task logs have the same limit as a container's shard and count towards the total.

## Pushed Updates

The UI keeps one Socket.IO connection to the manager (`/api/socket.io`) and follows topics over it instead of polling: the manager's status (`info`), the workers (`workers`, and one worker as `worker:<hostname>`), an application (`application:<project>/<name>`), the domains (`domains`), the tasks (`tasks`, and the finished ones as `task-history`), the backup and discovery status, the platform backups (`backups`), the release, the notifications and the logs (a task's, an application's and the requests). A page loads a topic from its REST route, and the manager pushes the whole state of a state topic each time it changes and each new row of a feed (a notification, a finished task) as it is stored; the page loads again each time the socket connects, so it makes up for what it missed. Socket.IO reconnects and falls back from WebSocket to long-polling by itself, a heartbeat every 10 s shows in an indicator at the top left of the UI, and a connection from another origin is refused. See [backend.md](backend.md) and [frontend.md](frontend.md).

What is pushed, and what the API keeps:
- **A topic is for changes a page did not make:** a background task, the manager's own checks, or another tab. Examples: an application's status while a deploy runs and when a container goes to error (`application:<project>/<name>`); a worker going offline or coming back (`workers`, `worker:<hostname>`); a settings check ending (`info`); a worker removed by its background task while its page is open (the topic answers 404 and the page shows not found).
- **A page's own synchronous action is shown by the page,** from the response or by loading again, and needs no topic. Examples: creating or deleting an application (the page that did it reloads or leaves, so a project's application list has no topic); emptying a settings section, which no check follows (the saving page reads `info` back, and another tab sees it at its next `info` push).
- **Where both fit** (a page's own action that another tab or another part of the page also shows), it is decided case by case, in the tracker item that meets it.
- **The API stays complete.** A route returns its whole resource whether or not a topic carries it, and a field is not dropped because the UI gets it from a push: `GET /workers/{hostname}` keeps `online`, though the UI reads `status`. What belongs to another resource is not copied in: `GET /api` has no worker count (the workers are `GET /workers`), and an application's containers name their worker without its state (`GET /workers/{hostname}`).

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
