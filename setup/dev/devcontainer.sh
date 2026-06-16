#!/bin/bash

set -e

sudo apt-get update -qq

# Setup Python venv
python3 -m venv app/app-venv
source app/app-venv/bin/activate
# Install Python dependencies
pip install -r app/requirements.txt

# Install UI dependencies
npm i --prefix ui