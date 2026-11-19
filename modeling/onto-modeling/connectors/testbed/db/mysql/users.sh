#!/bin/bash
set -euo pipefail
# 测试床账号（由 MySQL 镜像的初始化入口在建库后执行）：onto_ro 只读，onto_rw 能写，用来测「有写权限就拒绝采集」。
# 密码来自环境变量，不写进仓库。
mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER 'onto_ro'@'%' IDENTIFIED BY '${TESTBED_DB_RO_PASSWORD}';
GRANT SELECT, SHOW VIEW ON shop.* TO 'onto_ro'@'%';
GRANT SELECT, SHOW VIEW ON ops.* TO 'onto_ro'@'%';
GRANT SHOW_ROUTINE ON *.* TO 'onto_ro'@'%';
CREATE USER 'onto_rw'@'%' IDENTIFIED BY '${TESTBED_DB_RW_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'onto_rw'@'%';
SQL
