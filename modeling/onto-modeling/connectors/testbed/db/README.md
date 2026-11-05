# 数据库提供方测试床

合成的订单库，给 `db-mysql`、`db-postgres` 做集成测试和联调。数据全部编造：手机号中段是 `0000`，证件号地区码是 `99`，
邮箱在 `example.invalid`。

| | MySQL 8.0 | PostgreSQL 16 |
|---|---|---|
| 端口（只绑 127.0.0.1） | `TESTBED_MYSQL_PORT`，默认 33306 | `TESTBED_PG_PORT`，默认 55432 |
| 库 | `shop`、`ops` | 库 `shopdb`，schema `shop`、`ops` |
| 只读账号 | `onto_ro`（SELECT、SHOW VIEW、SHOW_ROUTINE） | `onto_ro`（USAGE + SELECT） |
| 能写的账号 | `onto_rw`（采集应被拒绝） | `onto_rw` |

`shop` 里有 7 张表（客户、商品、订单、明细、支付、发货，加一张没建外键的 `order_notes`），外键、唯一、检查约束，
视图 `v_customer_orders`，触发器 `trg_order_items_amount`，存储过程 `sp_cancel_order`，表和列注释。

```bash
cd modeling/onto-modeling/connectors/testbed/db
export TESTBED_DB_ROOT_PASSWORD=$(openssl rand -hex 12) TESTBED_DB_RO_PASSWORD=$(openssl rand -hex 12) \
       TESTBED_DB_RW_PASSWORD=$(openssl rand -hex 12)
docker compose up -d --wait
```

跑集成测试（在后端测试镜像里，经 `host.docker.internal` 连本机端口）：

```bash
docker run --rm -v "$PWD/../../../../..":/chg --add-host=host.docker.internal:host-gateway \
  -e F14_DB_MYSQL_HOST=host.docker.internal -e F14_DB_MYSQL_PORT=33306 \
  -e F14_DB_PG_HOST=host.docker.internal -e F14_DB_PG_PORT=55432 \
  -e F14_DB_RO_PASSWORD="$TESTBED_DB_RO_PASSWORD" -e F14_DB_RW_PASSWORD="$TESTBED_DB_RW_PASSWORD" \
  onto-test:py311 bash -c 'pip install -q PyMySQL pg8000 && cd /chg/modeling/onto-modeling/backend &&
    python -m pytest -q -p no:cacheprovider tests/test_f14_db_integration.py'
```

用完 `docker compose down -v`。
