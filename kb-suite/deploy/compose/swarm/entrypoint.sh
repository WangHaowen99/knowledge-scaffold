#!/bin/sh
# Configure the workspace (init, model settings), then start Swarm.
# Connector, expert and custom agents go through Swarm's public API, which
# needs the running service: provision_swarm.py waits for it in the background.
set -e
python /usr/local/bin/configure_swarm.py
python /usr/local/bin/provision_swarm.py &
exec jiuwenswarm-start all
