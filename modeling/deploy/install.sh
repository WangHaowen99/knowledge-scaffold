#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${BUNDLE_ROOT}/.env"
EXAMPLE_FILE="${BUNDLE_ROOT}/.env.example"

[ -f "${SCRIPT_DIR}/manifest.json" ] || {
    printf '[modeling-install][error] 发布清单不存在：%s\n' "${SCRIPT_DIR}/manifest.json" >&2
    exit 1
}
[ -f "${BUNDLE_ROOT}/checksums.txt" ] || {
    printf '[modeling-install][error] checksums.txt 不存在\n' >&2
    exit 1
}
(
    cd "${BUNDLE_ROOT}"
    sha256sum -c checksums.txt >/dev/null
) || {
    printf '[modeling-install][error] 发布包校验失败\n' >&2
    exit 1
}

if [ ! -f "${ENV_FILE}" ]; then
    [ -f "${EXAMPLE_FILE}" ] || {
        printf '[modeling-install][error] 配置模板不存在：%s\n' "${EXAMPLE_FILE}" >&2
        exit 1
    }
    cp "${EXAMPLE_FILE}" "${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"

persist_setting() {
    key="$1"
    value="$2"
    temp_file="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
    awk -v key="${key}" -v value="${value}" '
        BEGIN { updated = 0 }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            if (!updated) { print key "=" value; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print key "=" value }
    ' "${ENV_FILE}" > "${temp_file}"
    chmod 600 "${temp_file}"
    mv "${temp_file}" "${ENV_FILE}"
}

[ -z "${MODELING_PORT:-}" ] || persist_setting MODELING_PORT "${MODELING_PORT}"
[ -z "${MODELING_MCP_PORT:-}" ] || persist_setting MODELING_MCP_PORT "${MODELING_MCP_PORT}"

exec bash "${SCRIPT_DIR}/deploy.sh"
