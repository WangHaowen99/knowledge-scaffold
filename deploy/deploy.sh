#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--consumer-only|--modeling-only|--onto-modeling-only]

Deploy the full stack — consumer, Modeling Workbench and Onto Modeling — from
either a source checkout or an extracted release package.
EOF
}

log() {
    printf '[deploy] %s\n' "$*"
}

die() {
    printf '[deploy][error] %s\n' "$*" >&2
    exit 1
}

env_get() {
    local file="$1" key="$2" value
    value="$([ -f "${file}" ] && grep -E "^${key}=" "${file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

ensure_release_modeling_service_token() {
    local bundle_root="$1" env_file="${1}/backend/.env" example_file="${1}/backend/.env.example"
    local secret temp_file
    if [ ! -f "${env_file}" ]; then
        [ -f "${example_file}" ] || die "Consumer environment template not found: ${example_file}"
        cp "${example_file}" "${env_file}"
        chmod 600 "${env_file}"
    fi
    secret="${MODELING_SERVICE_TOKEN:-$(env_get "${env_file}" MODELING_SERVICE_TOKEN)}"
    if [ -z "${secret}" ]; then
        if command -v openssl >/dev/null 2>&1; then
            secret="$(openssl rand -hex 32)"
        elif command -v python3 >/dev/null 2>&1; then
            secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        else
            die "Cannot generate MODELING_SERVICE_TOKEN: openssl or Python 3 is required"
        fi
    fi
    [ "${#secret}" -ge 32 ] || die "MODELING_SERVICE_TOKEN must contain at least 32 characters"
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "Cannot update ${env_file}"
    chmod 600 "${temp_file}"
    awk -v secret="${secret}" '
        BEGIN { updated = 0 }
        /^MODELING_SERVICE_TOKEN=/ {
            if (!updated) { print "MODELING_SERVICE_TOKEN=" secret; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print "MODELING_SERVICE_TOKEN=" secret }
    ' "${env_file}" > "${temp_file}" || { rm -f "${temp_file}"; die "Cannot update ${env_file}"; }
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
    MODELING_SERVICE_TOKEN="${secret}"
    export MODELING_SERVICE_TOKEN
    log "Modeling service token is ready (value is not printed)"
}

ensure_release_modeling_mcp_token_secret() {
    local bundle_root="$1" env_file="${1}/backend/.env" example_file="${1}/backend/.env.example"
    local secret temp_file
    if [ ! -f "${env_file}" ]; then
        [ -f "${example_file}" ] || die "Consumer environment template not found: ${example_file}"
        cp "${example_file}" "${env_file}"
        chmod 600 "${env_file}"
    fi
    secret="${MODELING_MCP_TOKEN_SECRET:-$(env_get "${env_file}" MODELING_MCP_TOKEN_SECRET)}"
    if [ -z "${secret}" ]; then
        if command -v openssl >/dev/null 2>&1; then
            secret="$(openssl rand -hex 32)"
        elif command -v python3 >/dev/null 2>&1; then
            secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        else
            die "Cannot generate MODELING_MCP_TOKEN_SECRET: openssl or Python 3 is required"
        fi
    fi
    [ "${#secret}" -ge 32 ] || die "MODELING_MCP_TOKEN_SECRET must contain at least 32 characters"
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "Cannot update ${env_file}"
    chmod 600 "${temp_file}"
    awk -v secret="${secret}" '
        BEGIN { updated = 0 }
        /^MODELING_MCP_TOKEN_SECRET=/ {
            if (!updated) { print "MODELING_MCP_TOKEN_SECRET=" secret; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print "MODELING_MCP_TOKEN_SECRET=" secret }
    ' "${env_file}" > "${temp_file}" || { rm -f "${temp_file}"; die "Cannot update ${env_file}"; }
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
    MODELING_MCP_TOKEN_SECRET="${secret}"
    export MODELING_MCP_TOKEN_SECRET
    log "Modeling MCP token secret is ready (value is not printed)"
}

CONSUMER_ONLY=0
MODELING_ONLY=0
ONTO_MODELING_ONLY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --consumer-only)
            CONSUMER_ONLY=1
            ;;
        --modeling-only)
            MODELING_ONLY=1
            ;;
        --onto-modeling-only)
            ONTO_MODELING_ONLY=1
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

if [ "$(( CONSUMER_ONLY + MODELING_ONLY + ONTO_MODELING_ONLY ))" -gt 1 ]; then
    die "--consumer-only / --modeling-only / --onto-modeling-only cannot be combined"
fi

# A release archive is commonly unpacked into a staging directory by CI or a
# remote deployment runner. When an existing install is present elsewhere,
# dispatch through its upgrade flow so BUNDLE_ROOT and persistent data stay
# bound to the real installation directory.
INSTALL_DIR="${ONTO_AGENT_INSTALL_DIR:-/opt/onto-agent}"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -f "${SCRIPT_DIR}/manifest.json" ] && [ "${BUNDLE_ROOT}" != "${INSTALL_DIR}" ]; then
    EXISTING_INSTALL=false
    for marker in \
        "${INSTALL_DIR}/VERSION" \
        "${INSTALL_DIR}/deploy/state/current.env"; do
        if [ -e "${marker}" ]; then
            EXISTING_INSTALL=true
            break
        fi
    done
    if [ "${EXISTING_INSTALL}" = false ]; then
        for marker in "${INSTALL_DIR}/.onto-runtime" "${INSTALL_DIR}/.onto-auth"; do
            if [ -d "${marker}" ] && find "${marker}" -mindepth 1 \( -type f -o -type l \) -print -quit | grep -q .; then
                EXISTING_INSTALL=true
                break
            fi
        done
    fi
    if [ "${EXISTING_INSTALL}" = true ] && [ "${ONTO_MODELING_ONLY}" = "0" ]; then
        [ -f "${SCRIPT_DIR}/upgrade.sh" ] \
            || die "Existing installation detected but upgrade.sh is missing"
        log "Existing installation detected at ${INSTALL_DIR}; dispatching upgrade flow"
        UPGRADE_ARGS=("${INSTALL_DIR}")
        if [ "${CONSUMER_ONLY}" = "1" ]; then
            UPGRADE_ARGS+=(--consumer-only)
        elif [ "${MODELING_ONLY}" = "1" ]; then
            UPGRADE_ARGS+=(--modeling-only)
        fi
        exec bash "${SCRIPT_DIR}/upgrade.sh" "${UPGRADE_ARGS[@]}"
    fi
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
    if [ "${ONTO_MODELING_ONLY}" = "0" ]; then
        bash "${SCRIPT_DIR}/deploy-stack.sh" up "${DEPLOY_ARGS[@]}"
    fi
    if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
        log "Deploying Onto Modeling (source, docker compose)"
        (cd "${SCRIPT_DIR}/../modeling/onto-modeling" && docker compose up -d --build)
    fi
    log "Deployment finished successfully"
    exit 0
else
    die "Could not find source or release deployment scripts under ${SCRIPT_DIR}"
fi

BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ensure_release_modeling_service_token "${BUNDLE_ROOT}"
ensure_release_modeling_mcp_token_secret "${BUNDLE_ROOT}"

if [ "${MODELING_ONLY}" = "0" ] && [ "${ONTO_MODELING_ONLY}" = "0" ]; then
    [ -f "${CONSUMER_DEPLOY}" ] || die "Consumer deploy script not found: ${CONSUMER_DEPLOY}"
    log "Deploying consumer services"
    bash "${CONSUMER_DEPLOY}"
fi

if [ "${CONSUMER_ONLY}" = "0" ] && [ "${ONTO_MODELING_ONLY}" = "0" ]; then
    [ -f "${MODELING_DEPLOY}" ] || die "Modeling deploy script not found: ${MODELING_DEPLOY}"
    log "Deploying Modeling Workbench (core + Agent)"
    bash "${MODELING_DEPLOY}"
fi

ONTO_MODELING_DEPLOY="${BUNDLE_ROOT}/onto-modeling/deploy.sh"
if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
    if [ -f "${ONTO_MODELING_DEPLOY}" ]; then
        log "Deploying Onto Modeling (guided modeling workbench)"
        bash "${ONTO_MODELING_DEPLOY}"
    elif [ "${ONTO_MODELING_ONLY}" = "1" ]; then
        die "Onto Modeling deploy script not found: ${ONTO_MODELING_DEPLOY}"
    else
        log "Onto Modeling bundle not present; skipping (older release package)"
    fi
fi

log "Deployment finished successfully"
