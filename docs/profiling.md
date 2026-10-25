# Profiling A Live Sage Process

The development image bakes in two live profilers so you can attach to the **running** manager via
`docker exec` — no restart, no code change. Use this to diagnose CPU spikes or memory
growth in place.

- **py-spy** — CPU sampling and thread stacks (read-only, very safe).
- **memray** — memory allocation tracking (injects via `gdb`; slightly more invasive).

Both, and `gdb`, are installed only in the development image (`app/Dockerfile` with
`ENV=development`; the profilers come from `requirements-dev.txt`). The published image
doesn't carry them. Attaching needs the `SYS_PTRACE` capability, which only the
development compose file (`docker-compose.yml`) grants. The capability is
set at container creation, so it must be present **before** the incident — you can't
add it to a running container without recreating it (which would destroy the live state
you want to capture).

PID `1` is the container entrypoint (`app/entrypoint.sh`); the manager is its child.
Look up the manager's PID first:

```bash
PID=$(docker exec sage pgrep -of '^python.*main\.py')
```

Write outputs under `/app/data` (the `data` volume) and copy them out with
`docker cp sage:/app/data/<file> .`.

## Memory: what is leaking? (memray)

Capture allocations for a window while memory is climbing, then render a
**leak** view (allocations still resident at the end of the window):

```bash
# Attach, auto-detach after 20 min, trace Python-object allocations.
docker exec sage memray attach "$PID" -o /app/data/mem.bin \
  --duration 1200 --trace-python-allocators --force

# Render the leak flamegraph once the capture finishes.
docker exec sage memray flamegraph --leaks /app/data/mem.bin \
  -o /app/data/mem-leaks.html --force
docker cp sage:/app/data/mem-leaks.html .
```

The widest frames are the leaking call sites. Text alternatives:

```bash
docker exec sage memray stats /app/data/mem.bin
docker exec sage memray tree  /app/data/mem.bin
```

Tips:
- Run the capture **during** an active climb so the growth lands in the window.
- If the leak view points into a C boundary (sqlite, asyncssh, cryptography)
  rather than clear Python code, re-attach adding `--native` for C/C++ frames.

## Leak vs. fragmentation (check both views)

RSS growth is not always a leak. Always look at **both**:

- `memray flamegraph --leaks` (or the leak-filtered views) = allocations **still
  resident** at the end. If this is small but RSS keeps climbing, it is **not a
  classic leak.**
- `memray stats` = **cumulative churn** (total allocations / top allocators by
  count). A huge churn with a tiny live peak (`Peak memory usage`) means the RSS
  growth is **heap fragmentation** driven by high-frequency alloc/free — the
  allocator holds freed pages instead of returning them to the OS.

A real example from this codebase (the worker metrics poll, since replaced by Telegraf's reports): it created a new `httpx.Client`
every minute per host, each rebuilding an SSL context and reloading the whole CA
bundle (~1.9M allocations, all freed) — `--leaks` showed almost nothing, but
`stats` showed the churn. The fix was reusing one client. The
general pattern: **don't recreate expensive clients/sessions on hot paths**
(httpx, boto3 clients, SSH connections) — build once and reuse.

## Fragmentation mitigations (already applied)

- **`MALLOC_ARENA_MAX=2`** (set in the Dockerfiles): the thread-pool lanes
  otherwise make glibc spawn many arenas that fragment and inflate RSS.
- If RSS still creeps after killing the churn and capping arenas, try jemalloc
  via `LD_PRELOAD` (fragments less, returns memory to the OS more readily).

## CPU: what is it doing? (py-spy)

```bash
docker exec sage py-spy dump --pid "$PID"                             # all thread stacks, now
docker exec sage py-spy top  --pid "$PID"                             # live hot functions
docker exec sage py-spy record --pid "$PID" --duration 60 -o /app/data/cpu.svg  # flamegraph
```

`dump` is the fastest "what is it stuck on"; `record` gives an aggregated
flamegraph of where CPU time went.

## Notes

- Nothing here opens a network port; the only trigger is `docker exec`, reachable
  only by someone who can already exec into the container.
- `SYS_PTRACE` is scoped to sage's own PID namespace (it can't reach the host or
  other containers).
- If `memray attach` fails to inject, confirm `gdb` is present and the capability
  is set (`docker inspect sage --format '{{.HostConfig.CapAdd}}'`).
