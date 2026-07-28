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
  └─ evaluation           在“数据源配置”中连接并上传 Excel 的项目评测库
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
**约定 scene = 数据库名**（例如 `example_domain` 或项目配置的 `evaluation`）。

```bash
cd modeling   # .env 需含 MODELING_LLM_*、EVAL_DB_URL（与 modeling 服务共用），可选 NL2SQL_TOKEN/NL2SQL_PORT
npx tsx --env-file=.env scripts/nl2sql-service.ts
```

## 4. modeling（:5100）

```bash
cd modeling   # .env：MODELING_LLM_*、AUTH_ISSUER_URL=http://localhost:5055、
              #       CONSUMER_API_URL=http://localhost:5055
npx tsx --env-file=.env src/server.ts
```

登录用 consumer 账号（issuer 转发）。页面「⚙ 设置」里的 consumer 地址优先于 env——注意别残留冒烟测试的 stub 地址。进入项目“数据源配置”，填写评测工作空间 ID、NL2SQL 服务和 Scene。可在 Scene 右侧点击“使用项目专用数据库”：弹框展示隐藏密码后的服务端 `EVAL_DB_URL`，确认它与 NL2SQL 服务指向同一 MySQL 服务器，或仅为本次操作输入其他 URL；确认后幂等创建唯一固定库 `mv_shadow_<project_id>` 并回填 Scene，库已存在时直接复用。保存并测试通过后上传 Excel，并显式确认 Sheet 同名表替换；开始导入前会再次显示相同的数据库连接确认框，也可为本次导入输入其他 URL。自定义 URL 只存在于当前请求内，不写项目配置、不修改 `EVAL_DB_URL`，导入历史也不保存它。数据库账号需具备 `CREATE DATABASE`、建表、写入和原子重命名权限。上传接口接受文件后立即返回，由独立 Worker Thread 在后台全量解析和写入（无 2000 行上限），页面轮询任务直到成功或失败。MySQL 使用临时表和原子重命名，因此写入失败不会先删除原表。任务运行期间不能启动评测。

## 5. 链路验证清单

| 检查 | 命令/方式 | 预期 |
| --- | --- | --- |
| MySQL | `docker exec mw-eval-mysql mysqladmin ping -uroot -p<REDACTED>` | alive |
| NL2SQL | `POST :5600/api/v1/query {"question":"AO工序AO_OP0001的执行工位是什么？","scene":"example_domain"}` | data 含 `STATION01-工位1` |
| consumer 就绪 | `GET :5055/api/system-config/readiness` | `ready:true` |
| consumer 全链路 | 导入示例本体 zip → 为该本体配置 `nl2sql_template` 数据源（url=`http://localhost:5600`）→ `runs/sync` 同上问题 | final_answer 含 `STATION01-工位1` |
| modeling→consumer | `POST :5100/api/settings/consumer/test` | `reachable/authOk/modelReady` 全 true |
| modeling→评测库 | 项目“数据源配置”保存并测试 → 上传 Excel → 等待后台任务完成 | 同名 Sheet 表及全部数据行出现在 `EVAL_DB_URL` 服务器上的 Scene 数据库 |

（`business-query-testcases.json` 13 条业务推理题 + `data-query-testcases.json` 26 条数据查询题，
字段 `question` / `ground_truth`，可直接录入“评测用例录入”节点）。
