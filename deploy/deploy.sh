#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--consumer-only|--modeling-only]

Deploy all services by calling each service deploy script.
EOF
}

log() {
    printf '[deploy] %s\n' "$*"
}

die() {
    printf '[deploy][error] %s\n' "$*" >&2
    exit 1
}

CONSUMER_ONLY=0
MODELING_ONLY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --consumer-only)
            CONSUMER_ONLY=1
            ;;
        --modeling-only)
            MODELING_ONLY=1
            ;;
        -h|--help|help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "Unknown argument: $1"
            ;;
    esac
    shift
done

if [ "${CONSUMER_ONLY}" = "1" ] && [ "${MODELING_ONLY}" = "1" ]; then
    die "--consumer-only and --modeling-only cannot be used together"
fi

if [ -f "${SCRIPT_DIR}/deploy-consumer.sh" ]; then
    CONSUMER_DEPLOY="${SCRIPT_DIR}/deploy-consumer.sh"
    MODELING_DEPLOY="${SCRIPT_DIR}/modeling/deploy.sh"
else
    die "This deploy.sh is the release-package dispatcher. Build and extract a release package first, or use ${SCRIPT_DIR}/deploy-stack.sh for source deployment."
fi

if [ "${MODELING_ONLY}" = "0" ]; then
    [ -f "${CONSUMER_DEPLOY}" ] || die "Consumer deploy script not found: ${CONSUMER_DEPLOY}"
    log "Deploying consumer services"
    bash "${CONSUMER_DEPLOY}"
fi

if [ "${CONSUMER_ONLY}" = "0" ]; then
    [ -f "${MODELING_DEPLOY}" ] || die "Modeling deploy script not found: ${MODELING_DEPLOY}"
    log "Deploying modeling service"
    bash "${MODELING_DEPLOY}"
fi

log "Deployment finished successfully"
