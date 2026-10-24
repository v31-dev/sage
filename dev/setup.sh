#!/bin/bash

# Builds the local development environment, as your own user: Docker (only when it is missing,
# on Ubuntu, using sudo), then uv, Python and app/.venv, then nvm, Node (the version in
# ui/.nvmrc) and ui/node_modules.
# Safe to re-run: each step does only what has changed since the last run.

set -euo pipefail

UV_VERSION=0.12.24
PYTHON_VERSION=3.14
NVM_VERSION=v0.40.4

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your own user, not root: it installs into your home directory." >&2
  exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Docker"
if command -v docker > /dev/null; then
  echo "Docker is already installed."
elif [ "$(. /etc/os-release && echo "${ID}")" = "ubuntu" ]; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  echo "To use docker without sudo: sudo usermod -aG docker ${USER}, then log in again."
else
  echo "Docker is not installed and this is not Ubuntu: install it yourself." >&2
fi

echo "==> Python"
export PATH="${HOME}/.local/bin:${PATH}"
if ! command -v uv > /dev/null; then
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | UV_NO_MODIFY_PATH=1 sh
fi
venv=app/.venv
if ! "${venv}/bin/python" -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '${PYTHON_VERSION}'.split('.'))))" 2> /dev/null; then
  uv venv --python "${PYTHON_VERSION}" --clear "${venv}"
fi
uv pip install --python "${venv}/bin/python" -r app/requirements.txt -r app/requirements-dev.txt

echo "==> Node"
export NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"
if [ ! -s "${NVM_DIR}/nvm.sh" ]; then
  curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh" | bash
fi
cd ui
# nvm is not written for set -e and set -u
set +eu
. "${NVM_DIR}/nvm.sh"
nvm install || { echo "nvm could not install the Node version in ui/.nvmrc." >&2; exit 1; }
nvm use || { echo "nvm could not select the Node version in ui/.nvmrc." >&2; exit 1; }
set -eu
install_stamp="$(cat package.json package-lock.json | sha256sum | cut -d' ' -f1)-$(node --version)"
if [ "$(cat node_modules/.install-stamp 2> /dev/null)" != "${install_stamp}" ]; then
  npm ci --no-audit --no-fund
  echo "${install_stamp}" > node_modules/.install-stamp
else
  echo "ui/node_modules is up to date."
fi
cd ..

echo "==> Done"
echo "Python: source app/.venv/bin/activate"
echo "Node:   cd ui && nvm use"
