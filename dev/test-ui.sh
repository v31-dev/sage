#!/bin/bash

# Runs the UI tests in a Node container, for a machine without that Node.
# Arguments go to vitest: dev/test-ui.sh tests/lib --coverage.enabled=false
# node_modules is kept in SAGE_TEST_NODE_MODULES (default ~/.cache/sage-test-node-modules) and
# reinstalled when package.json, package-lock.json or the image change.

set -euo pipefail

ui_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../ui" && pwd)"
image="node:$(tr -d 'v \n' < "${ui_dir}/.nvmrc")-slim"
modules_dir="${SAGE_TEST_NODE_MODULES:-${HOME}/.cache/sage-test-node-modules}"
mkdir -p "${modules_dir}"
# Docker would create the mount point as root, and a root-owned node_modules breaks `npm ci` later
mkdir -p "${ui_dir}/node_modules"

tty=()
if [ -t 1 ]; then tty=(-t); fi

docker run --rm ${tty[@]+"${tty[@]}"} \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e NPM_CONFIG_UPDATE_NOTIFIER=false \
  -e DEPENDENCIES_HASH="$({ cat "${ui_dir}/package.json" "${ui_dir}/package-lock.json"; echo "${image}"; } | sha256sum | cut -d' ' -f1)" \
  -v "${ui_dir}":/ui \
  -v "${modules_dir}":/ui/node_modules \
  -w /ui \
  "${image}" bash -c '
set -e
if [ "$(cat node_modules/.dependencies-hash 2>/dev/null)" != "$DEPENDENCIES_HASH" ]; then
  echo "Installing the dependencies (first run, or package.json, package-lock.json or the image changed)..."
  npm ci --no-audit --no-fund
  echo "$DEPENDENCIES_HASH" > node_modules/.dependencies-hash
fi
exec npm test -- "$@"
' _ "$@"
