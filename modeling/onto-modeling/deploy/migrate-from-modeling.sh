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
SNAPSHOT="/tmp/onto-migrate-$$.db"
mkdir -p "${STAGE}"
trap 'rm -rf "${STAGE}"; docker exec "${MODELING_CONTAINER}" rm -f "${SNAPSHOT}" >/dev/null 2>&1 || true' EXIT

echo "[1/3] 导出 ${MODELING_CONTAINER} 数据（modeling.db 一致性快照 + artifacts）..."
# Workbench 用 WAL 模式，已提交的数据可能还在 modeling.db-wal 里，单独复制主文件会读到空库。
# 用 Workbench 自带的 SQLite 做 VACUUM INTO：在读事务里合并 WAL 写出单文件快照，不打断在线写入
docker exec "${MODELING_CONTAINER}" node --no-warnings --experimental-sqlite -e '
const fs = require("node:fs");
const { DatabaseSync } = require("node:sqlite");
const [source, target] = process.argv.slice(1);
if (!fs.existsSync(source)) {
    console.error(`${source} 不存在`);
    process.exit(1);
}
fs.rmSync(target, { force: true });
const db = new DatabaseSync(source);
db.prepare("VACUUM INTO ?").run(target);
db.close();
' /data/modeling.db "${SNAPSHOT}"
docker cp "${MODELING_CONTAINER}:${SNAPSHOT}" "${STAGE}/modeling.db"
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
