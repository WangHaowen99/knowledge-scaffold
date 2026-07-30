#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "${SCRIPT_DIR}/"*.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_state

[ -n "${CURRENT_VERSION:-}" ] || die "No deployment found. Run deploy.sh first"
[ -n "${CURRENT_BACKEND_IMAGE:-}" ] || die "Current backend image not recorded in state"
[ -n "${CURRENT_FRONTEND_IMAGE:-}" ] || die "Current frontend image not recorded in state"
[ -n "${CURRENT_JIUWENBOX_IMAGE:-}" ] || CURRENT_JIUWENBOX_IMAGE="$(manifest_get jiuwenbox_image)"
[ -n "${CURRENT_JIUWENBOX_IMAGE:-}" ] || die "Current JiuwenBox image not recorded in state"

log "Restarting ${CURRENT_VERSION} (down + up)"

compose_down "${CURRENT_BACKEND_IMAGE}" "${CURRENT_FRONTEND_IMAGE}" "${CURRENT_AUTH_IMAGE:-}" "${CURRENT_JIUWENBOX_IMAGE}"
compose_up "${CURRENT_BACKEND_IMAGE}" "${CURRENT_FRONTEND_IMAGE}" "${CURRENT_AUTH_IMAGE:-}" "${CURRENT_JIUWENBOX_IMAGE}"

wait_for_container "onto-agent-backend" 180 || die "Backend container did not become healthy after restart"
wait_for_container "onto-agent-mcp" 180 || die "MCP container did not become healthy after restart"
wait_for_container "onto-agent-frontend" 180 || die "Frontend container did not become healthy after restart"
wait_for_container "onto-agent-auth-server" 180 || die "Auth server container did not become healthy after restart"
wait_for_container "onto-agent-jiuwenbox" 180 || die "JiuwenBox container did not become healthy after restart"

log "Restart finished successfully"
