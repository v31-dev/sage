#!/usr/bin/env bash
# Starts the manager's own Tailscale node in userspace networking mode (no TUN device
# or host socket needed), then the manager process. The container exits when either
# of them does.
set -euo pipefail

: "${TS_AUTH_KEY:?TS_AUTH_KEY must be set}"
: "${TS_NAME:?TS_NAME must be set}"
: "${ENCRYPTION_KEY:?ENCRYPTION_KEY must be set}"
: "${TS_WORKER_TAG:?TS_WORKER_TAG must be set}"
: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN must be set}"
: "${DOMAIN:?DOMAIN must be set}"

state_dir=/app/data/tailscale
mkdir -p "$state_dir" /var/run/tailscale

# Tailscale's default MTU plus WireGuard overhead exceeds some network paths, where
# large transfers then stall while small ones succeed.
export TS_DEBUG_MTU=1230

tailscaled --tun=userspace-networking --statedir="$state_dir" &
tailscaled_pid=$!

# Sage's log line format, for the messages that come before Sage's own logger starts.
critical() {
  printf '[%s] [CRITICAL] [%-19s] [] %s\n' "$(date '+%Y-%m-%d %H:%M:%S,%3N')" entrypoint "$1" >&2
}

# Nothing works without the tailnet (the workers, the manager's DNS record), so a node that
# can't join stops the container at once. tailscaled is stopped first so that its output
# doesn't bury the message.
fail_tailnet() {
  kill -TERM "$tailscaled_pid" 2>/dev/null || true
  wait "$tailscaled_pid" 2>/dev/null || true
  critical "$1"
  exit 1
}

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
# lost state, or the machine was removed from the tailnet); on a restart of a node that is
# already logged in, a used or expired key is never looked at. The node's tags come from
# the key.
if [ "$backend_state" = "NeedsLogin" ]; then
  tailscale up --auth-key="$TS_AUTH_KEY" --hostname="$TS_NAME" --timeout=120s \
    || fail_tailnet "Could not join the tailnet with TS_AUTH_KEY. Check that the key is valid, not expired and tagged, and that the machine doesn't wait for approval in the Tailscale admin console."
fi

tailnet_ip=""
for _ in $(seq 1 30); do
  tailnet_ip=$(tailscale ip -4 2>/dev/null) && [ -n "$tailnet_ip" ] && break
  sleep 1
done
[ -n "$tailnet_ip" ] \
  || fail_tailnet "This node is not on the tailnet (state: ${backend_state:-unknown}). Check TS_AUTH_KEY, and that tailscaled can reach Tailscale."

# TS_NAME names the machine on every boot, so changing it renames the node — unless
# auto-generating the name is turned off for it in the Tailscale admin console.
tailscale set --hostname="$TS_NAME" \
  || echo "entrypoint: could not set the tailnet machine name to TS_NAME" >&2

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
