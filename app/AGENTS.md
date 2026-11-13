# AGENTS — app (manager backend)

What must hold in the manager backend. Read the root [../AGENTS.md](../AGENTS.md) first: its working rules apply
here and are not repeated. How the backend works (the queue's modes, each workflow's steps, the task catalog) is in
[docs/backend.md](../docs/backend.md) and [docs/operations.md](../docs/operations.md), linked from each section.

## Networking (userspace Tailscale)

- Inbound tailnet traffic to the manager is delivered to `127.0.0.1:<port>` inside the container, so anything
  listening there (on `0.0.0.0` or `127.0.0.1`) is reachable from the tailnet wherever the tailnet policy allows.
  Never put an internal-only listener on `127.0.0.1`; use `127.0.0.2` or a unix socket.
- The container has no route into the tailnet: SSH to workers goes through `tailscale nc` (asyncssh `proxy_command`),
  and for HTTP the workers reach the manager (Telegraf's reports and Vector's logs on 9001), not the other way round.
  Internet calls (Cloudflare, S3, ACME, GitHub) go direct.
- `sync_file` creates every file with its mode under a temporary name and renames it into place, so a reader never
  finds a partial file. A file with a secret in it goes with `mode=0o600` (a script `0o700`; the default is `0o644`):
  Tailscale SSH ignores the mode of a new directory, so the file's own mode is what keeps it private.

## Code Layout

- A resource's package under `services/` (`applications`, `containers`, `domains`, `workers`, `backups`) holds its
  concept by role: its writes in `commands.py`; its decisions (status, refusals, corrections) in `decisions.py`, pure
  functions of explicit facts; the in-memory owner of its observed facts and evidence, and its payload builders, in
  `state.py` (the containers' owner in `targets.py`, an application's payload in `payload.py`); what it writes
  outside the database in `rendering.py` or `config.py`; what tells its pages in `updates.py`. A rule or value about
  one resource lives in its package; a new kind of thing goes in the package that owns it.
- The Manager mixins (`services/manager/`) are the workflows: the `request_*` functions (admission and enqueue), the
  task bodies (effects in order, follow-up requests) and the readers (`*_states`, `*_payloads`, `*_page`, `*_list`,
  `*_graphs`) that gather the owners' facts for a decision. They call the resource's rules, never restate them.
- A question several tasks or readers ask of a resource (whether a container can be routed to, whether Sage can act
  on a worker) is one decision in its package, which every gate and reader uses; none composes it again from the
  owners' raw facts.
- A route holds no policy: it calls the command or the request function for the request and returns the reader's
  payload or the task's id, catching none of the standard errors (Database And Commands).

## Services, Memory And Updates

- Services are singletons on the base in `services/base.py`, with thread-safe mutations. Do not introduce another
  service lifecycle pattern without a clear reason.
- In-memory state derived from the DB or settings (setting values, the S3 client, notification config, the worker
  list) is refreshed wherever that data is replaced: a settings save, a restore.
- A topic is computed again when one of its sources fires, and an owner fires a source only when what it holds
  differs, so every state a topic shows needs a source.
- A Fact's callback runs under the Fact's lock: it publishes or requests work and never checks a Fact. An owner checks
  a Fact, enqueues or publishes only outside its own lock. So locks nest one way (a state Fact, the queue's facts, the
  queue's lock), and nothing runs under the queue's lock.
- `GET /api` is the `info` topic, and the pause banner and the restore overlay rely on it, so it reads memory only: no
  network, no database.
- A Socket.IO topic is for a change the page did not make, so name that change before proposing one; a page's own
  action is shown by the page. The REST API stays complete: a route keeps every field whether or not a topic carries
  it. The rule with examples: [Pushed Updates](../docs/architecture.md#pushed-updates).

## Database And Commands

- Main state is SQLite in WAL mode. The database is deferred: `Database` opens it at `config.DB_PATH` at startup, and
  importing the models touches no disk. Preserve this lightweight storage unless a task requires otherwise.
- Models declare schema, storage conversion and constraints. Save hooks and signals perform no cross-resource effects:
  dependent fields, membership, counters and unlinks belong to the resource's command.
- A resource command (built on `services/db/writes.py`) runs in its own `db.atomic("IMMEDIATE")` transaction and
  refuses to run inside another; it re-fetches by id, merges the patch's explicit keys and writes the affected fields;
  it publishes only after its commit. It raises `ValueError` for an invalid value, `LookupError` for a missing row and
  `utils.errors.Conflict` for a duplicate or a refused delete, which `app/api.py` answers 400, 404 and 409 (a
  `KeyError` or `IndexError` stays a 500).
- A page's payload is built by its resource's `*_payload` function from the state a Manager reader decides; REST and
  the topics share those readers, and a missing resource is a `LookupError`.
- Storage fields belong to `services/db/fields.py`, encryption to `services/db/encryption.py`, name and origin rules
  to `values/names.py`, key/value rules to `values/key_values.py`; scalar rules are shared, not repeated in routes and
  models. Importing a value or codec initializes no encryption and never exits the process: bootstrap validates and
  initializes the key before encrypted storage is used. The DB package initializer loads no models or signals;
  schemas and initialization are imported from their owning modules.
- Everything Sage writes outside its database (worker files, markers, DNS record comments, archives) is named by the
  id of the resource that owns it, with no instance prefix, number or mutable name: an opaque random id (stable across
  renames and recreation, distinct between Sages), or for a worker its hostname, the Worker's primary key and unique
  in the tailnet.
- A Container id is an opaque placement identity: kept across Deploy, Stop and Manager restart, fresh on deletion and
  recreation. `Instance.id` establishes ownership; `Instance.state_generation` establishes the installed
  configuration's authority, stays stable in ordinary operation and is fresh in a restored database before its owners
  load. Target evidence must match both, so old-generation work cannot authorize resuming runtime after a restore.
- A Volume rename keeps its identity and data; worker storage and backup association follow the id, not the name.
- Container and Worker events survive their resource's deletion until history retention: they keep their subject's
  identity and context apart from the live row, are never reassigned to a recreated resource and are never runtime
  authority.
- Times are naive UTC, the way the UI reads a timestamp without a zone; read the clock through
  `utils.common.utcnow()` (ruff's `DTZ003` and `DTZ005` reject the others). Retry with backoff goes through
  `utils.retry` (`retry`, `retry_async`, and `is_transient` as the one meaning of transient); a provider's minimum
  wait is the error's numeric `retry_after_seconds`, which both helpers honor without adding attempts.
- Metrics are per-host SQLite files; logs are a SQLite file per application with FTS5 search, Traefik's access lines
  as requests in columns (`logs/requests.db`) and a queued task's lines in `logs/tasks.db`, read by task id. The
  manager's other lines are in stdout only (`docker logs`).
- The live database (`data-gitignore/data.db`) is only ever read: open it with
  `sqlite3.connect('file:...?mode=ro', uri=True)` and never run INSERT, UPDATE, DELETE or DDL against it. To exercise
  a write path, copy it to the session's scratch space and point `config.DB_PATH` at the copy.

## Resource Contracts

Each resource's own rules, on top of the root's working rules 9–13. The workflows behind them are in
[docs/backend.md](../docs/backend.md).

### Containers

- Headline status and runtime knowledge are separate. When the worker is unreachable and no usable current
  observation remains, the headline is error, the runtime unknown or unreachable, and the last observation is kept,
  never taken as proof that the process stopped or still runs.
- A Deploy whose worker is unavailable is refused without a stop, a restart or invalidating target proof. A Stop that
  cannot reach its worker fails and needs an explicit retry; no deferred Stop intent is kept.
- Once a manual lifecycle action has started, its failure calls for a best-effort stop and headline error, which a
  running observation does not erase. An unexpected exit or absence after a committed deployment is error, with no
  automatic restart.
- Docker health is an independent runtime fact: it changes neither the headline nor routing. Keep the deployment
  command's `--wait` readiness check.
- An interrupted volume restore leaves error and its details, and an explicit Deploy is allowed once old work has
  settled; no restore-before-deploy interlock.
- A manual backup needs the container stopped. An automatic backup keeps the runtime it found: it pauses with
  `docker compose stop` and resumes with `start`, so the same Docker containers come back (never `down`/`up`, which
  recreate them), restores the applied deployment rather than newer saved config, and reports its own failure apart
  when the runtime came back. Its intent and the prior runtime are persisted in the target record, so a restart
  restores them too; an incomplete automatic backup is not handled as an interrupted deploy.
- Crash recovery uses the existing bounded worker command timeouts and normal reconciliation, with no PID tracking or
  targeted cancellation. Recovery may repeat containment stops; a new remote mutation on the target waits until old
  work has settled and the runtime is verified. A live task never releases its scope while its children still run.

### Workers

- `Workers` owns configured membership, infrastructure facts and writes, and the read model. Observed address,
  reachability and path are source-owned memory; the Worker row holds hostname and subdomain. A worker's projection
  reads certificate state and never changes desired identities or material.
- Readiness needs a fresh coherent report and positive Traefik evidence; the startup grace never grants it alone.
- A reboot succeeds only on a changed host boot id and a ready worker. A missed deadline stays a failed task; later
  proof of that reboot and readiness returns the worker online, with the failure kept and no automatic second reboot.
- Disruptive infrastructure changes on a worker exclude that worker's hosted container work only; observations and
  other workers' work go on.
- The one manual repair is a worker's resync: its forced setup, which then requests the forced copies of what else
  Sage keeps on it (certificates, its Domains' routes, discovery). Shared records are corrected by the scheduled
  passes, so there is no platform-wide resync. A corrective action is defined by its owned effects and verified goal,
  preserves other owners' evidence and uses the same operations as automatic convergence.
- Adding a worker is an explicit user action, so it cleans up, where required, what an earlier worker of that hostname
  left.

### Domains And Discovery

- A Domain owns its routing and DNS apply result and its artifacts' identity; the results are memory held by
  `Domains`, and `syncing` comes from the queue. `Domain.container` owns a name's binding: use `Container.domains`,
  with no application-level domain collection. Domain changes and queue state tell the linked application's and
  worker's views.
- An apply never holds its container's scope, and checks its worker and name again before any I/O. Its route follows
  the container's `routable` at the apply, and unknown removes nothing. It certifies only the fingerprint it applied;
  an edit during it queues another.
- The Domain pass and worker setup remove a routing file only by binding (its Domain gone or bound to another worker),
  never for its container's state; a route left behind is withdrawn by requesting the pass. The pass never awaits the
  applies it requests, and a failed pass leaves a Domain's successful apply proof alone.
- A failed apply does not retry itself: its end requests discovery only, and the passes retry. A failed saved edit
  withdraws outdated owned routing where reachable and reports pending or error, leaving the container's runtime
  alone; it checks against the current configuration first, keeps artifacts already correct, and never takes unknown
  or unreachable cleanup for confirmed removal.
- `cors_origins`: blank derives the record's own current origin, explicit entries are maintained by hand, and the
  effective list is part of `DomainConfig` and its proof. Only a dirty origins field is normalized, so unrelated writes
  keep a concurrent edit. Application CORS stays apart from the shared discovery and probe policy.
- Discovery stays in sync with the Domain table: at every publication it lists every name with a service, with its
  pool, whatever is known of it. Only a name's `online` flag is liveness and can flap: decided from the Domain's
  state, its container's `routable` and its worker's readiness when a worker's files are written. It never
  reconciles status, probes or requests an apply, and is requested only through `request_discovery`.
- Scopes, the pass's order, what requests discovery, and the file formats: [Domain Apply And
  Discovery](../docs/backend.md#domain-apply-and-discovery).

### Certificates

- `Certs` owns desired identities, material, retirement and certificate status. A worker's inputs hold only the proof
  maps it applied; validity, expiry and errors come from `Certs`, for the worker wildcard and shared discovery apart.
- Issue and revoke only in the renewal (`renew_certificates`), one certificate at a time under `certificate`; the
  manager's own bootstrap runs serially before serving. Worker setup requests renewal or distribution and never copies
  material itself.
- Retired public leaves are kept until revocation is acknowledged or they expire, with no retirement queue, table or
  timer of their own. Certificate templates belong to the certificate proofs, not to infrastructure stamps.

### Backups And Restore

- Storage work takes one `S3().session()` at its start (a `Conflict` when S3 is not configured, so a user action gets
  its 409) and uses it to the end, its result included. A Backup row records the `storage_id` (`S3Session.identity`)
  it was written to, and work admitted under another identity is refused, never retargeted to the configured store.
- A store change invalidates the old store's backups: they are not listed and their rows go at the next discovery,
  the old store is no longer Sage's to manage, and the new store is reconciled afresh, blank or not.
- A platform backup is a coherent SQLite configuration snapshot, without history or Backup rows, taken while
  operations continue.
- A platform restore is a pseudo restart: install, clear the memory of the old configuration, `request_startup`; the
  history starts empty. It keeps the current S3 settings as an explicit input, read before the download and written
  before the owners load; the other settings come from the backup. What it does to workers: [Platform Backup And
  Restore](../docs/architecture.md#platform-backup-and-restore).

### Settings

- Optional services (S3, notifications) are checked before use: a scheduled trigger skips quietly when unconfigured,
  a user action gets a clear 4xx. Cloudflare is always configured; add no checks for it.
- Save validates locally, persists and checks the provider asynchronously. Save success, the provider check's outcome
  and queue activity are separate facts, and a result certifies only the configuration snapshot it checked. An
  explicit Test evaluates its candidate values and declared test effects without saving them.
- A section's consumer (S3, Notifications) is installed from `Settings().snapshot(key)` by the workflow that saved or
  restored it, keeps the revision it installed and ignores an older one, and never reads Settings itself. A check
  result is recorded with the revision it checked (`Settings().record_check`) and counts only while that revision is
  current.

## Operation Queue And Scheduler

The queue's modes, outcomes and states: [Operation Queue](../docs/backend.md#operation-queue) and [Pause And
Stop](../docs/backend.md#pause-and-stop).

- Each scheduled job only calls one of the Manager's request functions (`request_*`), which owns the task's
  `add_task`; routes call the same functions. Only `main.py` imports `scheduler`. All execution and concurrency
  control live in `TaskQueue` (`utils/queue.py`).
- A task is declared once, at its enqueue definition: its `key`, `scopes`, `executor`, `on_conflict` and `labels`.
  Add or update its row in the [Task Catalog](../docs/operations.md#task-catalog) with its triggers and model writes,
  and the route, startup and model-index entries of that reference when those paths change.
- A task's key is its request: its operation, then its subject's ids, then its intent; requests that take turns on one
  subject share one, and a weaker background request and a stronger explicit one get different keys. Its scopes are
  the resources it locks, hierarchically, always including its subject's own; its name is what it does; its params
  are its arguments; its executor is its capacity. Admission compares keys, never scopes: a task with another key on a
  conflicting scope defers a new one, never refuses it. A reader of the queue (a fact, a precondition, a payload) finds
  a subject's requests by key and a resource's holders by scope, takes the kind of work from the name, and never reads
  identity from params. Scope roots are declared in `services/manager/queue.py` and lanes in `utils/executor.py`,
  independently. `common` is single-thread and low priority, so nothing urgent goes there.
- Only the dispatcher starts tasks, on the loop. A queued task cannot start while its scopes conflict with a running
  task or with a task queued before it, either way (a parent before its child, a child before its parent); tasks that
  do not conflict start together. Nothing relies on when a queued task starts or on the order of tasks that do not
  conflict: order comes from scopes, or from a body requesting its follow-up at its end.
- A DEDUP request that finds its task running is absorbed by it: right for a repeat of the same request, and for one
  that carries a change only with a periodic pass behind it; a change that must be seen after a running task uses
  REPLACE. A scope built from a binding that can change before the task runs (a Domain's worker, the store) is checked
  again at its start, before any effect, and the task does nothing under a binding that moved.
- Derive exclusions from the complete effect inventory, inline calls and writers outside the queue included; a
  direct configuration edit needs guarded results independently of queue scopes. A resource Sage writes outside its
  database has one scope, built by its package, and every task that writes it holds that scope or an ancestor, so its
  writers exclude each other. A resource under two roots (a Domain's route on a worker, a worker's certificate files)
  is held on both sides by a writer that changes it; a writer that holds a single side only removes.
- `params` are JSON-safe ids and flags, recorded and shown; frozen execution objects (a deploy snapshot, an S3
  session) go in `inputs`, never recorded or pushed; `labels` name the subjects the way the UI does
  (`{"Application": "shop / web"}`), taken when queued. `quiet=True` is for scheduled housekeeping only.
- A task body takes ids and immutable request values, never ORM objects. It re-fetches identity and membership at
  execution without replacing a frozen snapshot with newer config. A row deleted before the task runs is a legitimate
  interleaving: `get_or_none`, then log and return quietly; so is an observation the request relied on that no longer
  holds (a report, a listing, a worker's reachability): check it again at the start and return quietly.
- An operation's variants (backup, offline, startup) are part of its declared contract, not exception branches or a
  second mutable registry; a refused pre-check is recorded as rejected, never as a failed lifecycle transition.
- Task shape: one async task that `asyncio.gather`s its offloaded leaves, one `add_task` per entity, or sequential
  `await`s; a fan-out parent is async ([Choosing A Task Shape](../docs/backend.md#choosing-a-task-shape)).
- The queue's facts only publish: activity is shown and admitted on, never a trigger for work. Work is requested by a
  task body's end, a state Fact's callback (an observed change) or a scheduled pass.
- Scheduler cadence lives in `app/scheduler.py`: cron jobs fire on wall-clock instants, and the `_AT_STARTUP` list
  also runs once right after startup. Sub-minute pumps use `IntervalTrigger`, everything else
  `CronTrigger.from_crontab`. APScheduler's `from_crontab` reads numeric day-of-week `0` as **Monday**, while the
  `croniter`-matched volume-backup crons read `0` as Sunday: be explicit about the weekday. `dispatch_tick` stays
  `async` (a sync job runs in a worker thread, where its `asyncio.create_task` has no running loop).
- A scheduled or recurring task's external calls are budgeted (Cloudflare allows 1,200 calls per 5 minutes per user,
  across everything that user does): one list per check rather than a lookup per item, and writes only on drift. A
  task a user's action starts runs rarely and isn't optimized for the rate limit.
- Sage never restarts or upgrades itself. Pause, restore and stop are queue states. While not running, `app/api.py`
  refuses changes with 503 (every method but GET, HEAD and OPTIONS, except pause and resume), so a route that only
  reads is a GET. A change route writes straight after that check and anything that calls a provider goes in a task,
  so no change lands after a restore's install. A change during which another request starts a restore gets the
  same 503 after it ran, since the restore replaces what it wrote; a pause or stop meanwhile leaves its answer. Pause comes only before a shutdown or an upgrade, so nothing else has
  to work while paused.
- When touching orchestration, verify the task flow, the logging flow and the async boundaries, including overlapping
  calls and a cache replaced while another thread reads it.

## Async And `task_id`

- `task_id` flows through a `ContextVar` (`utils/logging.py`): the queue's `run_task` sets it for each task (a request
  has none) and the offload helpers copy the context. Keep that propagation intact; never pass `task_id` as a
  parameter.
- Blocking network or remote I/O is offloaded with the helpers in `utils/executor.py`, never `asyncio.to_thread()`
  (the default executor, outside the lane pools): `run_in_executor_with_context(...)`, awaited, onto the task's lane
  or an explicit pool (it raises without one); `submit_with_context(...)`, fire-and-forget, with no running loop
  needed. A task's lane is declared at `add_task`; its offloads inherit it and name no pool.
- Never block a sync task on its own lane's pool: it deadlocks a single-worker lane.
- Local Peewee and SQLite calls run inline. Heavy file I/O (bulk writes in a loop, backup and restore copies,
  encryption, large listings) is offloaded and written in one transaction (`db.atomic()`, `insert_many`).

## Migrations And One-Off Changes

- No one-time migration, backfill or convergence code (DB rows, files, or external state such as Cloudflare records)
  to bring existing installs in line with a change; the manual step goes in the summary or PR. New writes carry the new
  desired state; reconciling what exists is a documented manual action.
- The one automatic migration is schema evolution: missing columns are added at boot and columns no model has are
  dropped (`Database`, `services/db/database.py`), in one transaction with foreign keys off. Anything beyond that is
  a manual step unless the user says otherwise.
- The upgrade is the release notes': what an existing install goes through on its way to a change (a record gone
  before its replacement is written, a step to run first) never decides where code runs or in what order. Design for
  the final state.
- A schema change that existing data can refuse (a new unique index, say) stops the boot with one `[CRITICAL]` line
  holding what is known of the failure and a link to the running version's release; the code prints no remediation,
  so the release notes can correct it without a release. Changing an existing column's type, `NOT NULL`, primary key,
  foreign key or `CHECK` (a new `ChoiceField` value included) is refused the same way on every existing database,
  since SQLite can't alter them in place; name its manual step in the release notes.

## Testing

- The core engine's tests (the queue, the reconcilers, the status decisions) are exhaustive and may be slow: real
  scenarios on the real class (deploys and syncs crossing on shared scopes, restores, pauses, shutdowns) with mock task
  bodies, seeded random delays and failures, producers on several threads, and an independent model that predicts
  each admission and dispatch decision. Check the invariants and the model at every step, and plant bugs to see the
  tests fail. Correct admission is the main criterion and scope exclusion the safety property. Mocks stand in for task
  bodies only, never for the decisions under test.
- A contract two layers share (a control's availability and the request's admission, a restore and the cleanup that
  follows it) gets a test across them; consistent unit tests on each side do not settle it.
- A change that depends on how Sage is reached is tested through that shape: uvicorn serving TLS itself, as in
  production, and the Traefik image from `docker-compose.yml` terminating TLS in front of the real app (locally: on the
  host network, with a self-signed certificate and a router like the one in `dev/traefik/dynamic.yml`). Connect the
  way a browser does, send the proxy's headers per request type, and say what was and wasn't exercised. A "not
  verified" line in the docs is unfinished work.
- Check a change to `app/templates/worker/vector/vector.yml` with the workers' Vector image instead of guessing at VRL
  ([Vector Config](../docs/development.md#vector-config)).
