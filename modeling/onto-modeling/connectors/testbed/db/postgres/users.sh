#!/bin/bash
set -euo pipefail
# 测试床账号（由 PostgreSQL 镜像的初始化入口在建库后执行）：onto_ro 只读，onto_rw 能写，用来测「有写权限就拒绝采集」。
# 密码来自环境变量，不写进仓库。
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname "${POSTGRES_DB:-shopdb}" \
  -v ro_pw="${TESTBED_DB_RO_PASSWORD}" -v rw_pw="${TESTBED_DB_RW_PASSWORD}" <<'SQL'
CREATE ROLE onto_ro LOGIN PASSWORD :'ro_pw';
GRANT USAGE ON SCHEMA shop, ops TO onto_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop, ops TO onto_ro;
CREATE ROLE onto_rw LOGIN PASSWORD :'rw_pw';
GRANT USAGE ON SCHEMA shop, ops TO onto_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA shop TO onto_rw;
SQL
