# 真实端到端环境搭建（consumer + NL2SQL + 评测库）

建模工具评测闭环的真实数据链路：

```
modeling (:5100)
  │  登录转发 / 影子工作区 / import-zip / data-source / runs·sync
  ▼
consumer (:5055，Flask，真实 LLM)
  │  agentic run → business_data_search → nl2sql_template provider
  ▼
NL2SQL 服务 (:5600，scripts/nl2sql-service.ts，LLM 生成 SELECT)
  │
  ▼
MySQL (:3306，Docker mw-eval-mysql)
  ├─ example_domain         示例样例 48 表（examples 数据集）
  └─ mw_proj_<项目id>     modeling 评测库落库（EVAL_DB_URL）
```

## 1. MySQL（Docker）

```bash
docker run -d --name mw-eval-mysql -e MYSQL_ROOT_PASSWORD=root -p 3306:3306 mysql:8
# 导入示例样例（48 表 / 309 行）
docker exec mw-eval-mysql mysql -uroot -p<REDACTED> -e "CREATE DATABASE IF NOT EXISTS example_domain CHARACTER SET utf8mb4;"
docker exec -i mw-eval-mysql mysql -uroot -p<REDACTED> example_domain \
```

## 2. consumer 后端（:5055）

```bash
cd consumer/backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # 至少填 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
# 关键：本机有系统代理（如 Privoxy）时必须给 localhost 放行，
# 否则 provider 调 NL2SQL 会被代理吞掉返回 503
NO_PROXY='localhost,127.0.0.1' no_proxy='localhost,127.0.0.1' ./venv/bin/python run.py
```

- 端口 5055 硬编码在 `server_config.py`。
- 首次启动自动创建 `consumer/.onto-runtime/`，默认账号 admin / <REDACTED_INITIAL_PASSWORD>，
  **强制改密**（`PUT /auth/change-password`，新密码不能包含用户名）。
- 就绪检查：`GET /api/system-config/readiness`（需登录）→ `ready: true`。

## 3. NL2SQL 服务（:5600）

兼容 consumer `nl2sql_template` / `nl2sql_http` provider 契约
（`POST /api/v1/query|sql`、`GET /health`、Bearer 鉴权、`{success,data,row_count}`）。
**约定 scene = MySQL 数据库名**（`example_domain` 或 modeling 落库的 `mw_proj_*`）。

```bash
cd modeling   # .env 需含 MODELING_LLM_*、EVAL_DB_URL，可选 NL2SQL_TOKEN/NL2SQL_PORT
npx tsx --env-file=.env scripts/nl2sql-service.ts
```

## 4. modeling（:5100）

```bash
cd modeling   # .env：MODELING_LLM_*、AUTH_ISSUER_URL=http://localhost:5055、
              #       CONSUMER_API_URL=http://localhost:5055、EVAL_DB_URL=mysql://root:REPLACE_WITH_STRONG_PASSWORD@127.0.0.1:3306
npx tsx --env-file=.env src/server.ts
```

登录用 consumer 账号（issuer 转发）。页面「⚙ 设置」里的 consumer 地址
优先于 env——注意别残留冒烟测试的 stub 地址。

## 5. 链路验证清单

| 检查 | 命令/方式 | 预期 |
| --- | --- | --- |
| MySQL | `docker exec mw-eval-mysql mysqladmin ping -uroot -p<REDACTED>` | alive |
| NL2SQL | `POST :5600/api/v1/query {"question":"AO工序AO_OP0001的执行工位是什么？","scene":"example_domain"}` | data 含 `STATION01-工位1` |
| consumer 就绪 | `GET :5055/api/system-config/readiness` | `ready:true` |
| consumer 全链路 | 导入示例本体 zip → 建 `nl2sql_template` 连接（url=`http://localhost:5600`）→ 绑定 → `runs/sync` 同上问题 | final_answer 含 `STATION01-工位1` |
| modeling→consumer | `POST :5100/api/settings/consumer/test` | `reachable/authOk/modelReady` 全 true |

（`business-query-testcases.json` 13 条业务推理题 + `data-query-testcases.json` 26 条数据查询题，
字段 `question` / `ground_truth`，可直接录入节点 8）。
