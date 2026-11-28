# NL2SQL 服务与评测 MySQL

`modeling/nl2sql-service/` 提供两个容器，独立于 `deploy/` 的整栈构建和部署：

- `nl2sql-service`：NL2SQL HTTP 服务。读取 Scene 数据库的表结构，用模型把问题转成一条只读 `SELECT`，在 MySQL 上执行并返回结果。接口兼容 consumer 的 `nl2sql_http` 和 `nl2sql_template` 数据源。
- `nl2sql-mysql`：MySQL 8 评测库。一个 Scene 对应一个数据库。

## 目录

```text
nl2sql-service/
  src/server.ts        HTTP 服务、SQL 校验和 MySQL 查询
  tests/               node:test 单元测试
  Dockerfile           服务镜像
  docker-compose.yml   nl2sql-service + nl2sql-mysql
  deploy.sh            一键部署脚本
  .env.example         部署配置模板
```

## 一键部署

前置条件：Docker 与 Docker Compose v2。

```bash
cd modeling/nl2sql-service
cp .env.example .env               # 填 MODELING_LLM_BASE_URL、MODELING_LLM_MODEL，按需填 MODELING_LLM_API_KEY
bash deploy.sh
```

`deploy.sh` 依次完成：

1. `.env` 不存在时从 `.env.example` 复制，并把权限设为 600。
2. `EVAL_MYSQL_ROOT_PASSWORD`、`NL2SQL_DB_PASSWORD`、`NL2SQL_TOKEN` 为空时生成随机值写入 `.env`，不打印到终端；两个数据库密码含字母和数字以外的字符时直接退出。
3. 检查模型配置，缺失时直接退出，避免服务容器反复重启。
4. 设置了 `NL2SQL_EXTERNAL_NETWORK` 时生成 `docker-compose.override.yml`，只把 `nl2sql-service` 加入该网络，MySQL 不加入；未设置时删除这个文件。
5. 执行 `docker compose up -d --build`，等待 MySQL 就绪后重建只读账号 `nl2sql_reader`，再等待 NL2SQL 的健康检查通过。

重复执行 `deploy.sh` 会保留已生成的凭据和数据卷，只重建有变化的容器。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MODELING_LLM_BASE_URL` | 无，必填 | OpenAI 兼容接口地址，服务调用 `<地址>/chat/completions`。网关在宿主机上时用 `host.docker.internal` 代替 `localhost` |
| `MODELING_LLM_MODEL` | 无，必填 | 生成 SQL 的模型名 |
| `MODELING_LLM_API_KEY` | 空 | 以 `Authorization: Bearer` 发给模型接口 |
| `NL2SQL_TOKEN` | 自动生成 | 调用 NL2SQL 接口时必须带的 Bearer Token |
| `EVAL_MYSQL_ROOT_PASSWORD` | 自动生成 | MySQL root 密码，只用于建库和导入数据。自行设置时只用字母和数字 |
| `NL2SQL_DB_PASSWORD` | 自动生成 | NL2SQL 查询用的只读账号 `nl2sql_reader` 的密码，会拼进连接串。自行设置时只用字母和数字 |
| `NL2SQL_BIND_ADDRESS` | `127.0.0.1` | NL2SQL 宿主端口的绑定地址 |
| `NL2SQL_PORT` | `5600` | NL2SQL 的宿主端口 |
| `EVAL_MYSQL_BIND_ADDRESS` | `127.0.0.1` | MySQL 宿主端口的绑定地址，供本机导入数据 |
| `EVAL_MYSQL_PORT` | `3306` | MySQL 的宿主端口 |
| `NL2SQL_EXTERNAL_NETWORK` | 空 | `nl2sql-service` 要加入的外部 Docker 网络，通常是 consumer 的网络 |
| `NL2SQL_DEFAULT_SCENE` | `default` | 请求没带 `scene` 时查询的数据库 |

`MYSQL_ROOT_PASSWORD` 只在数据卷首次初始化时生效。数据卷建好后再改 `EVAL_MYSQL_ROOT_PASSWORD`，`deploy.sh` 会因 root 密码不一致而无法重建只读账号并报错退出；需要改 root 密码时，先在 MySQL 里改，再同步 `.env`。`NL2SQL_DB_PASSWORD` 可以直接修改，重新执行 `deploy.sh` 即生效。

## 接入 consumer

consumer 后端运行在容器里，访问宿主机的 `127.0.0.1` 访问不到本服务，需要让两边处在同一个 Docker 网络：

1. 用 `docker network ls` 找到 consumer 的网络。从 `consumer/` 目录启动的 compose 项目，网络名通常是 `consumer_onto-agent-network`。
2. 在 `.env` 设置 `NL2SQL_EXTERNAL_NETWORK=<网络名>`，重新执行 `bash deploy.sh`。
3. 在 consumer 的知识包数据源里选择 `NL2SQL HTTP` 或 `NL2SQL Template`：
   - 服务地址：`http://nl2sql-service:5600`
   - 访问 Token：`.env` 中的 `NL2SQL_TOKEN`
   - Scene：要查询的 MySQL 数据库名

consumer 在其他机器上时，把 `NL2SQL_BIND_ADDRESS` 改为本机对 consumer 可达的内网地址。服务只校验 Token，不要把端口暴露到公网；`EVAL_MYSQL_BIND_ADDRESS` 保持 `127.0.0.1`。

## 导入评测数据

一个 Scene 对应一个数据库。以下命令在容器内读取密码，不会把密码写进命令历史：

```bash
# 建库
docker exec nl2sql-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS example_scene CHARACTER SET utf8mb4"'

# 导入 SQL 文件
docker exec -i nl2sql-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" example_scene' < example_scene.sql
```

表结构有 60 秒缓存，导入新表后最多 60 秒生效。

## 接口

除 `/health` 外都要带 `Authorization: Bearer <NL2SQL_TOKEN>`。

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 无 | 健康检查，返回 `{"status":"ok"}` |
| POST | `/api/v1/query` | `{"question": "...", "scene": "..."}` | 模型生成 SQL 后执行 |
| POST | `/api/v1/sql` | `{"sql": "...", "scene": "..."}` | 直接执行给定 SQL，同样经过只读校验 |

成功时返回 `{"success": true, "data": [...], "row_count": 1, "sql": "..."}`；查询失败时 HTTP 状态仍为 200，返回 `{"success": false, "error": "..."}`。

只读校验规则：

- 只接受单条 `SELECT` 或 `WITH` 语句，拒绝多语句和写入、改结构类关键字。
- 拒绝 `LOAD_FILE(...)`、`INTO OUTFILE`、`INTO DUMPFILE` 等读写服务器文件的写法。
- 拒绝引用系统库（`mysql`、`sys`、`performance_schema`、`information_schema`），也不能把它们作为 `scene`。
- 没有 `LIMIT` 时追加 `LIMIT 200`，返回结果最多 200 行，单条查询超时 30 秒。
- 按真实表名纠正模型生成的表名大小写。

查询使用只读账号 `nl2sql_reader`：对业务库只有 `SELECT` 和 `SHOW VIEW` 权限，收回了 `mysql`、`sys` 库的权限，没有读写服务器文件的权限。MySQL 不允许收回 `performance_schema` 的查询权限，由上面的校验拦截。这个账号能读同一 MySQL 里的所有业务库，不同 Scene 之间不做隔离。

## 本地开发

需要 Node.js 20 及以上版本。

```bash
cd modeling/nl2sql-service
npm install
npm run typecheck
npm test
npm run build

# 本机启动：.env 需能连到 MySQL，EVAL_DB_URL 建议用只读账号，形如 mysql://nl2sql_reader:<password>@127.0.0.1:3306
npm run dev
```

本机直接运行时，服务读取 `EVAL_DB_URL`，默认监听 `127.0.0.1:5600`，可用 `NL2SQL_HOST`、`NL2SQL_PORT` 修改。

## 运维

```bash
cd modeling/nl2sql-service
docker compose ps
docker compose logs -f nl2sql-service
docker compose down        # 停止并删除容器，保留 MySQL 数据卷
```

`docker compose down -v` 会删除 MySQL 数据卷和其中的全部评测数据，执行前先备份。

## 从旧部署迁移

旧版 NL2SQL 和评测库随 Modeling Workbench 部署，容器名为 `mw-modeling-nl2sql` 和 `mw-eval-mysql`。迁移时注意：

- 新数据卷不会继承旧库数据。需要保留时，先从旧容器导出，再导入 `nl2sql-mysql`：

  ```bash
  docker exec mw-eval-mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --databases example_scene' > example_scene.sql
  docker exec -i nl2sql-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD"' < example_scene.sql
  ```

- 旧容器若仍占用 3306 端口，先停掉旧容器，或修改 `EVAL_MYSQL_PORT`。
- consumer 数据源里的服务地址和 Token 要改成新值。
