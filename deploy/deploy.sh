#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--consumer-only|--modeling-only]

Deploy the full stack, including the Modeling Workbench, from either a source
checkout or an extracted release package.
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

DEPLOY_ARGS=()
if [ "${CONSUMER_ONLY}" = "1" ]; then
    DEPLOY_ARGS+=(--consumer-only)
elif [ "${MODELING_ONLY}" = "1" ]; then
    DEPLOY_ARGS+=(--modeling-only)
fi

if [ -f "${SCRIPT_DIR}/deploy-consumer.sh" ]; then
    CONSUMER_DEPLOY="${SCRIPT_DIR}/deploy-consumer.sh"
    MODELING_DEPLOY="${SCRIPT_DIR}/modeling/deploy.sh"
elif [ -f "${SCRIPT_DIR}/deploy-stack.sh" ]; then
    log "Detected source checkout; deploying with deploy-stack.sh"
    bash "${SCRIPT_DIR}/deploy-stack.sh" up "${DEPLOY_ARGS[@]}"
    log "Deployment finished successfully"
    exit 0
else
    die "Could not find source or release deployment scripts under ${SCRIPT_DIR}"
fi

if [ "${MODELING_ONLY}" = "0" ]; then
    [ -f "${CONSUMER_DEPLOY}" ] || die "Consumer deploy script not found: ${CONSUMER_DEPLOY}"
    log "Deploying consumer services"
    bash "${CONSUMER_DEPLOY}"
fi

if [ "${CONSUMER_ONLY}" = "0" ]; then
    [ -f "${MODELING_DEPLOY}" ] || die "Modeling deploy script not found: ${MODELING_DEPLOY}"
    log "Deploying Modeling Workbench (core + Agent)"
    bash "${MODELING_DEPLOY}"
fi

log "Deployment finished successfully"
