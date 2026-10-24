#!/bin/bash

set -e

echo "Resetting the SAGE development environment"

# Stop and remove the containers.
docker compose down --remove-orphans

# Wipe the manager's data directory (SAGE_DATA) except its Tailscale state and issued
# cert, so a reset neither registers a new tailnet node (one-off auth keys) nor
# re-issues the cert (Let's Encrypt rate limits). Runs inside the container because
# the files are root-owned.
docker compose run --rm --no-deps --entrypoint sh sage -c \
  'find /app/data -mindepth 1 -maxdepth 1 ! -name tailscale ! -name certs -exec rm -rf {} +'
