#!/bin/bash
# 一键迁移：把原 modeling 服务的项目/资料/知识包数据搬进 onto-modeling。
# 在部署机上运行；两个容器都需在运行中。幂等，可重复执行（已迁移的自动跳过）。
#
# 用法：./migrate-from-modeling.sh [modeling容器名] [onto-modeling容器名] [附加参数...]
#   附加参数原样传给迁移脚本，如 --owner alice、--project pj_xxx

set -euo pipefail

MODELING_CONTAINER="${1:-mw-modeling}"
ONTO_CONTAINER="${2:-onto-modeling}"
shift $(( $# > 2 ? 2 : $# )) || true

# WSL 下 docker 可能是 Windows 进程（interop），/tmp 之类的绝对路径无法解析，
# staging 一律用当前目录下的相对路径
STAGE=".migrate-stage-$$"
mkdir -p "${STAGE}"
trap 'rm -rf "${STAGE}"' EXIT

echo "[1/3] 导出 ${MODELING_CONTAINER} 数据（modeling.db + artifacts）..."
docker cp "${MODELING_CONTAINER}:/data/modeling.db" "${STAGE}/modeling.db"
docker cp "${MODELING_CONTAINER}:/data/artifacts" "${STAGE}/artifacts" 2>/dev/null \
    || mkdir -p "${STAGE}/artifacts"

echo "[2/3] 注入 ${ONTO_CONTAINER} 容器..."
docker exec "${ONTO_CONTAINER}" rm -rf /tmp/modeling-data
docker cp "${STAGE}" "${ONTO_CONTAINER}:/tmp/modeling-data"

echo "[3/3] 执行迁移..."
docker exec "${ONTO_CONTAINER}" python3 scripts/migrate_from_modeling.py \
    --source /tmp/modeling-data "$@"
docker exec "${ONTO_CONTAINER}" rm -rf /tmp/modeling-data
echo "迁移完成。刷新 onto-modeling 项目列表即可看到「<项目名>（迁移）」。"
