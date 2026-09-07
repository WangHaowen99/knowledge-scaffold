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
Usage: $(basename "$0") [install-dir] [--consumer-only|--modeling-only]

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
shift || true
DEPLOY_ARGS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --consumer-only|--modeling-only)
            DEPLOY_ARGS+=("$1")
            ;;
        -h|--help|help)
            usage
            exit 0
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
    shift
done

if [ "${DEPLOY_ARGS[0]:-}" = "--consumer-only" ] && [ "${DEPLOY_ARGS[1]:-}" = "--modeling-only" ]; then
    die "--consumer-only and --modeling-only cannot be used together"
fi

if [ ! -d "${INSTALL_DIR}/deploy" ] || [ ! -f "${INSTALL_DIR}/VERSION" ]; then
    die "Not a valid installation directory: ${INSTALL_DIR} (expected deploy/ and VERSION to exist)"
fi

UPGRADE_LOCK_FILE="${INSTALL_DIR}/.consumer-deploy.lock"

validate_inherited_flock_deployment_lock() {
    local lock_file="$1"
    local lock_fd="${DEPLOY_TRANSACTION_LOCK_FD:-}"
    local lock_identity fd_identity confirmed_lock_identity

    case "${lock_fd}" in
        ''|*[!0-9]*)
            die "Inherited deployment transaction lock has an invalid file descriptor"
            ;;
    esac
    if [ "${lock_fd}" -lt 3 ]; then
        die "Inherited deployment transaction lock cannot use a standard I/O file descriptor"
    fi
    [ -d /proc/self/fd ] \
        || die "Linux /proc/self/fd is required to validate an inherited deployment transaction lock"
    [ -e "/proc/self/fd/${lock_fd}" ] \
        || die "Inherited deployment transaction lock file descriptor is closed"
    lock_identity="$(stat -Lc '%d:%i' "${lock_file}" 2>/dev/null)" \
        || die "Cannot identify the stable deployment transaction lock file"
    fd_identity="$(stat -Lc '%d:%i' "/proc/self/fd/${lock_fd}" 2>/dev/null)" \
        || die "Cannot identify the inherited deployment transaction lock file descriptor"
    [ "${fd_identity}" = "${lock_identity}" ] \
        || die "Inherited deployment transaction lock file descriptor does not reference the stable lock file"
    flock -n "${lock_fd}" \
        || die "Inherited deployment transaction lock is not held by this process"
    confirmed_lock_identity="$(stat -Lc '%d:%i' "${lock_file}" 2>/dev/null)" \
        || die "Cannot revalidate the stable deployment transaction lock file"
    [ "${confirmed_lock_identity}" = "${fd_identity}" ] \
        || die "Stable deployment transaction lock file changed during inherited lock validation"
}

acquire_upgrade_transaction_lock() {
    local lock_fd

    command -v flock >/dev/null 2>&1 \
        || die "util-linux flock is required for deployment transaction locking"
    if [ "${DEPLOY_TRANSACTION_LOCK_BACKEND:-}" = "flock" ] \
        && [ -n "${DEPLOY_TRANSACTION_LOCK_TOKEN:-}" ]; then
        validate_inherited_flock_deployment_lock "${UPGRADE_LOCK_FILE}"
        DEPLOY_TRANSACTION_LOCK_HELD=true
        export DEPLOY_TRANSACTION_LOCK_HELD
        return 0
    fi

    exec {lock_fd}> "${UPGRADE_LOCK_FILE}"
    if ! flock -n "${lock_fd}"; then
        eval "exec ${lock_fd}>&-"
        die "Another deployment transaction is already running"
    fi
    DEPLOY_TRANSACTION_LOCK_BACKEND=flock
    DEPLOY_TRANSACTION_LOCK_FD="${lock_fd}"
    DEPLOY_TRANSACTION_LOCK_TOKEN="$$-${RANDOM}-${RANDOM}"
    DEPLOY_TRANSACTION_LOCK_HELD=true
    export DEPLOY_TRANSACTION_LOCK_BACKEND
    export DEPLOY_TRANSACTION_LOCK_FD DEPLOY_TRANSACTION_LOCK_TOKEN
    export DEPLOY_TRANSACTION_LOCK_HELD
}

acquire_upgrade_transaction_lock

STATE_FILE="${INSTALL_DIR}/deploy/state/current.env"
BACKUP_ROOT="${INSTALL_DIR}/deploy/backups"

if [ ! -f "${STATE_FILE}" ]; then
    die "No previous deployment found in ${INSTALL_DIR}. Run deploy.sh for first-time install."
fi

CURRENT_VERSION=""
# shellcheck disable=SC1090
source "${STATE_FILE}"

[ -n "${CURRENT_VERSION:-}" ] || die "No previous deployment found in ${INSTALL_DIR}. Run deploy.sh for first-time install."
CURRENT_STORAGE_MODE="${CURRENT_STORAGE_MODE:-bind}"
case "${CURRENT_STORAGE_MODE}" in
    bind|volume)
        ;;
    *)
        die "Installed state has invalid CURRENT_STORAGE_MODE: ${CURRENT_STORAGE_MODE}"
        ;;
esac

log "Upgrading from ${CURRENT_VERSION}"
log "Install directory: ${INSTALL_DIR}"

PROTECTED_ITEMS=".env backend/.env backend/logs frontend/logs/nginx"
if [ "${CURRENT_STORAGE_MODE}" = "bind" ]; then
    PROTECTED_ITEMS+=" .onto-runtime .onto-auth"
else
    log "Pre-extract snapshot contains code/config only; authoritative volume data backup runs after writers stop in deploy.sh"
fi

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
tar \
    --exclude='./.consumer-deploy.lock' \
    -C "${RELEASE_ROOT}" -c -f - . \
    | tar -C "${INSTALL_DIR}" -xf -
chmod +x "${INSTALL_DIR}/deploy/"*.sh

log "Restoring protected files from backup"
for item in ${PROTECTED_ITEMS}; do
    archive="${PRE_BACKUP}/${item//\//_}.tar.gz"
    if [ -f "${archive}" ]; then
        tar -C "${INSTALL_DIR}" -xzf "${archive}"
    fi
done

log "Running deploy.sh to complete upgrade"
"${INSTALL_DIR}/deploy/deploy.sh" "${DEPLOY_ARGS[@]}"

log "Upgrade completed successfully"
