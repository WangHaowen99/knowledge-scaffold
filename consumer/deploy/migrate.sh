#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

FROM_VERSION="${1:-}"
TO_VERSION="${2:-$(manifest_get app_version)}"

# auth-server 数据目录尚无用户文件时，导入 consumer 存量用户。
# users.json 的 PBKDF2 哈希串自包含，原样导入即可，用户无需重设密码（架构决策 Consequences）。
# 与版本号无关（首次部署也要跑），因此放在版本迁移早退逻辑之前。
import_legacy_users_to_auth_server() {
    local source_users="${BUNDLE_ROOT}/.onto-runtime/users.json"
    local auth_data_dir="${BUNDLE_ROOT}/.onto-auth"
    local dest_users="${auth_data_dir}/users.json"

    if [ -f "${dest_users}" ]; then
        log "auth-server 用户数据已存在，跳过导入: ${dest_users}"
        return 0
    fi

    if [ ! -f "${source_users}" ]; then
        log "未发现存量用户文件 ${source_users}，跳过导入"
        return 0
    fi

    mkdir -p "${auth_data_dir}"
    cp "${source_users}" "${dest_users}"
    chmod 600 "${dest_users}"
    log "已导入存量用户到 auth-server 数据目录: ${dest_users}"
}

import_legacy_users_to_auth_server

if [ -z "${FROM_VERSION}" ] || [ "${FROM_VERSION}" = "${TO_VERSION}" ]; then
    log "No migration required"
    exit 0
fi

log "No custom migration steps defined for ${FROM_VERSION} -> ${TO_VERSION}"
exit 0
