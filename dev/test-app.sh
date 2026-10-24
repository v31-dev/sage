#!/bin/bash

# Runs the backend tests in a Python container, for a machine without that Python.
# Arguments go to pytest: dev/test-app.sh -q --no-cov -k queue
# The virtualenv is kept in SAGE_TEST_VENV (default ~/.cache/sage-test-venv) and rebuilt when
# the requirements or the image change.

set -euo pipefail

image="python:3.14-slim"
app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../app" && pwd)"
venv_dir="${SAGE_TEST_VENV:-${HOME}/.cache/sage-test-venv}"
mkdir -p "${venv_dir}"

tty=()
if [ -t 1 ]; then tty=(-t); fi

docker run --rm ${tty[@]+"${tty[@]}"} \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PIP_DEFAULT_TIMEOUT=60 \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -e REQUIREMENTS_HASH="$({ cat "${app_dir}/requirements.txt" "${app_dir}/requirements-test.txt"; echo "${image}"; } | sha256sum | cut -d' ' -f1)" \
  -v "${app_dir}":/app \
  -v "${venv_dir}":/venv \
  -w /app \
  "${image}" bash -c '
set -e
if [ "$(cat /venv/.requirements-hash 2>/dev/null)" != "$REQUIREMENTS_HASH" ]; then
  echo "Installing the test dependencies (first run, or the requirements or the image changed)..."
  python -m venv --clear /venv
  /venv/bin/pip install --quiet -r requirements.txt -r requirements-test.txt
  echo "$REQUIREMENTS_HASH" > /venv/.requirements-hash
fi
exec /venv/bin/pytest "$@"
' _ "$@"
