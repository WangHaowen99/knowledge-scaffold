#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${EVAL_ENV_FILE:?Set EVAL_ENV_FILE to a protected runtime configuration file}"
[ -f "$EVAL_ENV_FILE" ] || { echo 'Evaluation runtime configuration not found' >&2; exit 1; }
COMPOSE_ARGS=(-f "$SCRIPT_DIR/compose.yml")
if [ -n "${EVAL_COMPOSE_OVERRIDE:-}" ]; then
    [ -f "$EVAL_COMPOSE_OVERRIDE" ] || { echo 'Evaluation Compose override not found' >&2; exit 1; }
    COMPOSE_ARGS+=(-f "$EVAL_COMPOSE_OVERRIDE")
fi
if [ -f "$SCRIPT_DIR/../evaluation-image.tar" ]; then
    if [ -f "$SCRIPT_DIR/../IMAGE" ]; then
        EVAL_IMAGE="$(cat "$SCRIPT_DIR/../IMAGE")"
        export EVAL_IMAGE
    fi
    docker load -i "$SCRIPT_DIR/../evaluation-image.tar"
    docker compose "${COMPOSE_ARGS[@]}" up -d --no-build
else
    docker compose "${COMPOSE_ARGS[@]}" up -d --build
fi
