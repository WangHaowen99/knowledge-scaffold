#!/bin/sh
# Prepare the Swarm workspace (init, model + MCP config, plugin) and start it.
set -e
python /usr/local/bin/configure_swarm.py
exec jiuwenswarm-start all
