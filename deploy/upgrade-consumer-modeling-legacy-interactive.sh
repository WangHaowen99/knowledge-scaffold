#!/usr/bin/env bash
# Upgrade a legacy directory and migrate merged Auth ownership when needed.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ask() {
    local prompt="$1" answer
    printf '%s' "${prompt}" >/dev/tty
    IFS= read -r answer </dev/tty || exit 1
    [ -n "${answer}" ] || { printf '输入不能为空\n' >&2; exit 1; }
    printf '%s' "${answer}"
}

legacy_dir="$(ask '请输入旧合并部署根目录（将自动迁移 Auth 所有权）：')"
consumer_package="$(ask '请输入新版 Consumer 发布包路径：')"
auth_package="$(ask '请输入独立 Auth 发布包路径：')"
onto_package="$(ask '请输入 Onto Modeling 发布包路径：')"
hub_package="$(ask '请输入 Knowledge Hub 发布包路径：')"
evaluation_package="$(ask '请输入评测发布包路径：')"
existing_onto_env="$(dirname "${legacy_dir}")/onto-modeling/.env"
existing_port=""
if [ -f "${existing_onto_env}" ]; then
    existing_port="$(sed -n 's/^ONTO_WEB_PORT=//p' "${existing_onto_env}" | tail -n 1)"
fi

if [ -n "${existing_port}" ]; then
    onto_port="${existing_port}"
    printf '沿用现有 Onto Modeling 宿主端口：%s\n' "${onto_port}" >/dev/tty
else
    onto_port="$(ask '请输入 Onto Modeling 宿主端口：')"
fi
case "${onto_port}" in
    ''|*[!0-9]*) printf 'Onto Modeling 宿主端口必须为 1 到 65535 的整数\n' >&2; exit 1 ;;
esac
[ "${onto_port}" -ge 1 ] && [ "${onto_port}" -le 65535 ] \
    || { printf 'Onto Modeling 宿主端口必须为 1 到 65535 的整数\n' >&2; exit 1; }

ONTO_WEB_PORT="${onto_port}" bash "${script_dir}/install-consumer-modeling.sh" \
    --upgrade-from "${legacy_dir}" \
    --consumer-package "${consumer_package}" \
    --auth-package "${auth_package}" \
    --onto-modeling-package "${onto_package}" \
    --knowledge-hub-package "${hub_package}" \
    --evaluation-package "${evaluation_package}"
