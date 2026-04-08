#!/bin/bash

set -e

# Load environment variables from .env file
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

apt-get update -qq

# Install Docker
install -m 0755 -d /etc/apt/keyrings
curl -sSL --fail-with-body https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch="$(dpkg --print-architecture)" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu "$(. /etc/os-release && echo "${VERSION_CODENAME}")" stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker and Docker Compose plugins
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Install Python venv
apt install -y -qq python3.12-venv
python3 -m venv app/app-venv
source app/app-venv/bin/activate
# Install Python dependencies
pip install -r app/requirements.txt

# Install NodeJS
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
\. "${HOME}/.nvm/nvm.sh"
nvm install 24
# Install UI dependencies
npm i --prefix ui