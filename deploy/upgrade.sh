#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() {
    printf '[upgrade] %s\n' "$*"
}

die() {
    printf '[upgrade][error] %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [install-dir]

Upgrade onto-agent from this release package.

  install-dir    Installation directory (default: /opt/onto-agent)

Steps:
  1. Pre-extract backup of config, data and runtime
  2. Overlay new release files into install-dir
  3. Restore protected files from backup
  4. Run deploy.sh to complete the upgrade

Examples:
  $(basename "$0")                           # default /opt/onto-agent
  $(basename "$0") /data/onto-agent          # custom install directory
EOF
}

INSTALL_DIR="${1:-/opt/onto-agent}"

if [ ! -d "${INSTALL_DIR}/deploy" ] || [ ! -f "${INSTALL_DIR}/VERSION" ]; then
    die "Not a valid installation directory: ${INSTALL_DIR} (expected deploy/ and VERSION to exist)"
fi

STATE_FILE="${INSTALL_DIR}/deploy/state/current.env"
BACKUP_ROOT="${INSTALL_DIR}/deploy/backups"

if [ ! -f "${STATE_FILE}" ]; then
    die "No previous deployment found in ${INSTALL_DIR}. Run deploy.sh for first-time install."
fi

CURRENT_VERSION=""
# shellcheck disable=SC1090
source "${STATE_FILE}"

[ -n "${CURRENT_VERSION:-}" ] || die "No previous deployment found in ${INSTALL_DIR}. Run deploy.sh for first-time install."

log "Upgrading from ${CURRENT_VERSION}"
log "Install directory: ${INSTALL_DIR}"

PROTECTED_ITEMS="backend/.env backend/config backend/logs frontend/logs/nginx .onto-runtime"

log "Creating pre-extract backup"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
PRE_BACKUP="${BACKUP_ROOT}/pre-extract-${TIMESTAMP}"
mkdir -p "${PRE_BACKUP}"

for item in ${PROTECTED_ITEMS}; do
    if [ -e "${INSTALL_DIR}/${item}" ]; then
        tar -C "${INSTALL_DIR}" -czf "${PRE_BACKUP}/${item//\//_}.tar.gz" "${item}"
    fi
done
log "Pre-extract backup saved to ${PRE_BACKUP}"

log "Overlaying new release files"
tar -C "${RELEASE_ROOT}" -c -f - . | tar -C "${INSTALL_DIR}" -xf -
chmod +x "${INSTALL_DIR}/deploy/"*.sh

log "Restoring protected files from backup"
for item in ${PROTECTED_ITEMS}; do
    archive="${PRE_BACKUP}/${item//\//_}.tar.gz"
    if [ -f "${archive}" ]; then
        tar -C "${INSTALL_DIR}" -xzf "${archive}"
    fi
done

log "Running deploy.sh to complete upgrade"
"${INSTALL_DIR}/deploy/deploy.sh"

log "Upgrade completed successfully"
