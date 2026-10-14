#!/usr/bin/env bash
# Starts the manager's own Tailscale node in userspace networking mode (no TUN device
# or host socket needed), then the manager process. The container exits when either
# of them does.
set -euo pipefail

: "${TS_AUTH_KEY:?TS_AUTH_KEY must be set}"
: "${TS_NAME:?TS_NAME must be set}"

state_dir=/app/data/tailscale
mkdir -p "$state_dir" /var/run/tailscale

# Tailscale's default MTU plus WireGuard overhead exceeds some network paths, where
# large transfers then stall while small ones succeed.
export TS_DEBUG_MTU=1230

tailscaled --tun=userspace-networking --statedir="$state_dir" &
tailscaled_pid=$!

backend_state=""
for _ in $(seq 1 30); do
  backend_state=$(tailscale status --json 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["BackendState"])' 2>/dev/null) || true
  if [ -n "$backend_state" ] && [ "$backend_state" != "NoState" ]; then
    break
  fi
  sleep 1
done

# The auth key is only used when the node has no tailnet identity yet (first boot,
# lost state, or the machine was removed from the tailnet); a used or expired key
# never blocks a restart. The node's tags come from the key.
if [ "$backend_state" = "NeedsLogin" ]; then
  tailscale up --auth-key="$TS_AUTH_KEY" --hostname="$TS_NAME" --timeout=120s \
    || echo "entrypoint: tailscale up failed; provide a valid TS_AUTH_KEY" >&2
fi

"$@" &
app_pid=$!

trap 'kill -TERM "$app_pid" 2>/dev/null' TERM INT

set +e
wait -n "$app_pid" "$tailscaled_pid"
status=$?

# Without tailscaled the manager can't reach its workers, so its exit counts as a
# failure for "restart on failure" policies even when it exited cleanly.
if ! kill -0 "$tailscaled_pid" 2>/dev/null; then
  echo "entrypoint: tailscaled exited" >&2
  [ "$status" -eq 0 ] && status=1
fi

# Let the app finish shutting down before the tailnet goes away.
kill -TERM "$app_pid" 2>/dev/null
wait "$app_pid"
kill -TERM "$tailscaled_pid" 2>/dev/null
wait "$tailscaled_pid"
exit "$status"
