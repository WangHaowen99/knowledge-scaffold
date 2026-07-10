#!/bin/bash

# Run onto-agent with Docker Compose
# Usage: ./run_docker.sh <tag> [all|backend|frontend]
# The all and backend targets start the standalone MCP server alongside Flask.

set -euo pipefail

TAG=${1:-latest}
TARGET=${2:-all}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ensure_dir_writable() {
    local dir="$1"
    mkdir -p "$dir"
    if [ ! -w "$dir" ]; then
        echo "Error: directory is not writable: $dir"
        ls -ld "$dir" || true
        echo "Try: sudo chown -R $(id -un):$(id -gn) \"$dir\""
        exit 1
    fi
}

ensure_dir_exists() {
    local dir="$1"
    mkdir -p "$dir"
}

docker_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        printf 'docker compose'
        return 0
    fi

    if command -v docker-compose >/dev/null 2>&1; then
        printf 'docker-compose'
        return 0
    fi

    echo "Error: Docker Compose is required but not available (tried 'docker compose' and 'docker-compose')" >&2
    exit 1
}

COMPOSE_CMD="$(docker_compose_cmd)"

echo "Running with tag: $TAG, target: $TARGET"
echo "Using compose command: $COMPOSE_CMD"

export TAG=$TAG

ensure_env() {
    local env_file="$1"
    local example_file="$2"
    if [ ! -f "$env_file" ]; then
        if [ -f "$example_file" ]; then
            cp "$example_file" "$env_file"
            echo "Auto-created $env_file from $(basename "$example_file"). Please review and configure it."
        else
            echo "Error: $env_file not found and no $(basename "$example_file") available."
            exit 1
        fi
    fi
}

case "$TARGET" in
    all)
        ensure_env "$SCRIPT_DIR/backend/.env" "$SCRIPT_DIR/backend/.env.example"
        ensure_dir_exists "$SCRIPT_DIR/backend/config"
        ensure_dir_writable "$SCRIPT_DIR/.onto-runtime/config"
        ensure_dir_writable "$SCRIPT_DIR/backend/logs"
        ensure_dir_writable "$SCRIPT_DIR/frontend/logs/nginx"
        $COMPOSE_CMD -f "$SCRIPT_DIR/docker-compose.yml" --project-directory "$SCRIPT_DIR" up -d --build
        ;;
    backend)
        ensure_env "$SCRIPT_DIR/backend/.env" "$SCRIPT_DIR/backend/.env.example"
        ensure_dir_exists "$SCRIPT_DIR/backend/config"
        ensure_dir_writable "$SCRIPT_DIR/.onto-runtime/config"
        ensure_dir_writable "$SCRIPT_DIR/backend/logs"
        $COMPOSE_CMD -f "$SCRIPT_DIR/backend/docker-compose.yml" --project-directory "$SCRIPT_DIR/backend" up -d --build
        ;;
    frontend)
        ensure_dir_writable "$SCRIPT_DIR/frontend/logs/nginx"
        $COMPOSE_CMD -f "$SCRIPT_DIR/frontend/docker-compose.yml" --project-directory "$SCRIPT_DIR/frontend" up -d --build
        ;;
    *)
        echo "Error: Invalid target '$TARGET'. Use 'all', 'backend', or 'frontend'."
        exit 1
        ;;
esac
