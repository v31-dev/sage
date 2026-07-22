#!/bin/bash

set -e

# Load environment variables from .env file
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

: "${SAGE_HOME:?SAGE_HOME is not set}"

echo "Setting up development environment for SAGE at ${SAGE_HOME}"

# Stop and remove any existing containers
docker compose down --volumes --remove-orphans

# Reset SAGE_HOME but keep the manager's cert dir (issued cert + ACME account
# key) so a dev reset doesn't re-issue and burn Let's Encrypt rate limits.
mkdir -p "${SAGE_HOME}/sage/certs"
find "${SAGE_HOME}" -mindepth 1 -maxdepth 1 ! -name sage -exec rm -rf {} +
find "${SAGE_HOME}/sage" -mindepth 1 -maxdepth 1 ! -name certs -exec rm -rf {} +

# Update .env with the Tailscale info
TS_IP=$(tailscale ip -4)
grep -q "^HOSTNAME=" .env && sed -i "s/^HOSTNAME=.*/HOSTNAME=$(hostname)/" .env || echo "HOSTNAME=$(hostname)" >> .env
grep -q "^TS_IP=" .env && sed -i "s/^TS_IP=.*/TS_IP=${TS_IP}/" .env || echo "TS_IP=${TS_IP}" >> .env