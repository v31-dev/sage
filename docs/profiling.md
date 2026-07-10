# Profiling A Live Sage Process

Sage bakes in two live profilers and grants the `SYS_PTRACE` capability so you
can attach to the **running** process via `docker exec` — no restart, no code
change. Use this to diagnose CPU spikes or memory growth in place.

- **py-spy** — CPU sampling and thread stacks (read-only, very safe).
- **memray** — memory allocation tracking (injects via `gdb`; slightly more invasive).

Both are installed in the image; `cap_add: ["SYS_PTRACE"]` on the `sage` service
enables attaching. The capability is set at container creation, so it must be
present **before** the incident — you can't add it to a running container without
recreating it (which would destroy the live state you want to capture).

The container's main process is PID `1` (`python main.py`). Write outputs under
`/app/data` — it's the mounted data volume, so files appear on the host at
`${SAGE_HOME}/sage/…` with no `docker cp`.

## Memory: what is leaking? (memray)

Capture allocations for a window while memory is climbing, then render a
**leak** view (allocations still resident at the end of the window):

```bash
# Attach, auto-detach after 20 min, trace Python-object allocations.
docker exec sage memray attach 1 -o /app/data/mem.bin \
  --duration 1200 --trace-python-allocators --force

# Render the leak flamegraph once the capture finishes.
docker exec sage memray flamegraph --leaks /app/data/mem.bin \
  -o /app/data/mem-leaks.html --force
```

Open `${SAGE_HOME}/sage/mem-leaks.html` (or `docker cp sage:/app/data/mem-leaks.html .`).
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

A real example from this codebase: `Metrics.collect` created a new `httpx.Client`
every minute per host, each rebuilding an SSL context and reloading the whole CA
bundle (~1.9M allocations, all freed) — `--leaks` showed almost nothing, but
`stats` showed the churn. The fix was reusing one client (see `self._http`). The
general pattern: **don't recreate expensive clients/sessions on hot paths**
(httpx, `aioboto3.Session`, SSH connections) — build once and reuse.

## Fragmentation mitigations (already applied)

- **`MALLOC_ARENA_MAX=2`** (set in the Dockerfiles): the thread-pool lanes
  otherwise make glibc spawn many arenas that fragment and inflate RSS.
- If RSS still creeps after killing the churn and capping arenas, try jemalloc
  via `LD_PRELOAD` (fragments less, returns memory to the OS more readily).

## CPU: what is it doing? (py-spy)

```bash
docker exec sage py-spy dump --pid 1                              # all thread stacks, now
docker exec sage py-spy top  --pid 1                              # live hot functions
docker exec sage py-spy record --pid 1 --duration 60 -o /app/data/cpu.svg  # flamegraph
```

`dump` is the fastest "what is it stuck on"; `record` gives an aggregated
flamegraph of where CPU time went.

## Notes

- Nothing here opens a network port; the only trigger is `docker exec`, reachable
  only by someone who can already exec into the container.
- `SYS_PTRACE` is scoped to sage's own PID namespace (it can't reach the host or
  other containers). The incremental risk is negligible because the container
  already mounts the Docker socket.
- If `memray attach` fails to inject, confirm `gdb` is present and the capability
  is set (`docker inspect sage --format '{{.HostConfig.CapAdd}}'`).
