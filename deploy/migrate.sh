#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

FROM_VERSION="${1:-}"
TO_VERSION="${2:-$(manifest_get app_version)}"

if [ -z "${FROM_VERSION}" ] || [ "${FROM_VERSION}" = "${TO_VERSION}" ]; then
    log "No migration required"
    exit 0
fi

log "No custom migration steps defined for ${FROM_VERSION} -> ${TO_VERSION}"
exit 0
