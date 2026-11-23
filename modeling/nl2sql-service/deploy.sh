#!/bin/bash
# 一键部署 NL2SQL 服务与评测 MySQL：准备 .env → 按需接入外部 Docker 网络 → 构建并启动 → 等待健康检查通过。
# 不属于 deploy/ 的整栈部署，可在任意目录执行：bash modeling/nl2sql-service/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

log() {
    printf '[nl2sql-service] %s\n' "$*"
}

die() {
    printf '[nl2sql-service][error] %s\n' "$*" >&2
    exit 1
}

# 与 docker compose 的取值顺序一致：shell 环境变量优先，其次是 .env
config_value() { # <变量名>
    local name="$1"
    if [ -n "${!name:-}" ]; then
        printf '%s\n' "${!name}"
    else
        sed -n "s/^${name}=//p" .env | tail -n 1
    fi
}

ensure_secret() { # <变量名>：为空时生成 32 字节十六进制随机值写入 .env，不回显
    local name="$1" generated
    [ -n "$(config_value "${name}")" ] && return 0
    generated="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    sed -i.bak "/^${name}=/d" .env
    rm -f .env.bak
    # 手改过的 .env 可能缺结尾换行，直接追加会接到上一行末尾
    [ -z "$(tail -c 1 .env)" ] || printf '\n' >> .env
    printf '%s=%s\n' "${name}" "${generated}" >> .env
    unset generated
    log "已在 .env 生成 ${name}"
}

# NL2SQL 查询用的只读账号：每次部署重建，密码与 .env 同步。对全部库只读，收回 mysql、sys 系统库的权限；
# 密码经标准输入传入容器，root 密码取自容器自身的环境变量，都不出现在宿主命令行上
ensure_reader_account() {
    local password failure
    password="$(config_value NL2SQL_DB_PASSWORD)"
    failure="创建只读账号 nl2sql_reader 失败：确认 .env 的 EVAL_MYSQL_ROOT_PASSWORD 与数据卷初始化时的密码一致"
    docker exec -i nl2sql-mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot' <<EOF || die "${failure}"
DROP USER IF EXISTS 'nl2sql_reader'@'%';
CREATE USER 'nl2sql_reader'@'%' IDENTIFIED BY '${password}';
GRANT SELECT, SHOW VIEW ON *.* TO 'nl2sql_reader'@'%';
REVOKE SELECT, SHOW VIEW ON mysql.* FROM 'nl2sql_reader'@'%';
REVOKE SELECT, SHOW VIEW ON sys.* FROM 'nl2sql_reader'@'%';
EOF
    unset password
    log "已更新只读账号 nl2sql_reader"
}

wait_healthy() { # <容器名>
    local container="$1" status=""
    for _ in $(seq 1 60); do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${container}" 2>/dev/null || true)"
        [ "${status}" = "healthy" ] && return 0
        sleep 3
    done
    die "${container} 180 秒内未就绪（状态：${status:-unknown}），查看日志：docker compose -f ${SCRIPT_DIR}/docker-compose.yml logs ${container}"
}

command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（docker compose）"

if [ ! -f .env ]; then
    cp .env.example .env
    log "已从 .env.example 生成 .env"
fi
chmod 600 .env
ensure_secret EVAL_MYSQL_ROOT_PASSWORD
ensure_secret NL2SQL_DB_PASSWORD
ensure_secret NL2SQL_TOKEN
# 两个数据库密码会拼进连接串和 SQL，只允许字母和数字
for name in EVAL_MYSQL_ROOT_PASSWORD NL2SQL_DB_PASSWORD; do
    [[ "$(config_value "${name}")" =~ ^[A-Za-z0-9]+$ ]] || die "${name} 只能包含字母和数字，请修改 ${SCRIPT_DIR}/.env"
done

if [ -z "$(config_value MODELING_LLM_BASE_URL)" ] || [ -z "$(config_value MODELING_LLM_MODEL)" ]; then
    die "请先在 ${SCRIPT_DIR}/.env 填写 MODELING_LLM_BASE_URL 和 MODELING_LLM_MODEL，再重新执行"
fi

network="$(config_value NL2SQL_EXTERNAL_NETWORK)"
if [ -n "${network}" ]; then
    docker network inspect "${network}" >/dev/null 2>&1 \
        || die "Docker 网络 ${network} 不存在，检查 NL2SQL_EXTERNAL_NETWORK（docker network ls 查看）"
    cat > docker-compose.override.yml <<EOF
# 由 deploy.sh 按 NL2SQL_EXTERNAL_NETWORK 生成，勿手改；改 .env 后重跑 deploy.sh
services:
  nl2sql-service:
    networks:
      - default
      - external
networks:
  external:
    external: true
    name: ${network}
EOF
    log "nl2sql-service 将加入 Docker 网络 ${network}（MySQL 不加入）"
else
    rm -f docker-compose.override.yml
fi

docker compose up -d --build
wait_healthy nl2sql-mysql
ensure_reader_account
wait_healthy nl2sql-service

nl2sql_bind="$(config_value NL2SQL_BIND_ADDRESS)"
nl2sql_port="$(config_value NL2SQL_PORT)"
mysql_bind="$(config_value EVAL_MYSQL_BIND_ADDRESS)"
mysql_port="$(config_value EVAL_MYSQL_PORT)"
log "部署完成"
log "NL2SQL：http://${nl2sql_bind:-127.0.0.1}:${nl2sql_port:-5600}"
log "MySQL：${mysql_bind:-127.0.0.1}:${mysql_port:-3306}（导入数据用 root，密码见 .env 的 EVAL_MYSQL_ROOT_PASSWORD）"
[ -z "${network}" ] || log "同网络容器访问地址：http://nl2sql-service:5600"
log "访问 Token 见 ${SCRIPT_DIR}/.env 的 NL2SQL_TOKEN"
