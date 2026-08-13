# Meta Wise 全栈部署手册

> **本手册面向自动化 agent 与人类操作者。** 约定：
> - 每个步骤给出「执行目录 → 命令 → 成功判据」，成功判据均为可机器执行的命令与预期输出；
> - 所有命令幂等，重复执行安全；
> - `$REPO` 指代仓库根目录（即本文件所在 `deploy/` 的上一级）；
> - 编排脚本输出带 `[OK]` / `[WARN]` / `[FAIL]` 标记，出现 `[FAIL]` 即中止并打印修复指令，按指令处理后重跑同一命令即可。

## 0. 系统拓扑

| 服务 | 代码目录 | 宿主端口 → 容器端口 | 镜像 | 依赖 | 说明 |
| --- | --- | --- | --- | --- | --- |
| consumer-backend | `consumer/backend/` | 5055 → 5055 | onto-agent-backend | LLM 端点 | 运行期 API（Flask），知识包运行时 |
| consumer-frontend | `consumer/frontend/` | 8888 → 80 | onto-agent-frontend | backend | 知识工程控制台（nginx 托管） |
| modeling | `modeling/` | 5100 → 5100 | mw-modeling | consumer-backend | 知识包建模工作台（登录与评测均经 consumer） |
| auth-server（可选） | `auth-server/` | 不暴露宿主端口（容器网内 5056） | onto-agent-auth-server | — | 解耦鉴权（OAuth2/OIDC 子集），consumer 以 `AUTH_MODE=remote` 对接 |
| eval-mysql（可选） | — | 3306 → 3306 | mysql:8 | — | 建模评测库（Excel 数据自动落库供查询类用例） |

服务间调用链：`浏览器 → frontend(8888) → backend(5055)`；`modeling(5100) → backend(5055)`（登录转发 + 影子工作区评测）；`backend → auth-server(5056)`（仅 `AUTH_MODE=remote` 时）。所有容器同处一个 Docker 网络（`*onto-agent-network`）。

数据持久化位置：

| 数据 | 位置 | 备份方式见 |
| --- | --- | --- |
| consumer 运行时（知识包/工作区/会话/配置） | `consumer/.onto-runtime/`（宿主目录挂载） | 第 8 节 |
| 建模项目（SQLite） | docker 卷 `mw-modeling_modeling-data` | 第 8 节 |
| 评测库 | docker 卷 `mw-modeling_eval-mysql-data` | 第 8 节 |
| 鉴权数据（启用 auth 时） | `consumer/.onto-auth/`（宿主目录挂载） | 第 8 节 |

## 1. 部署形态选择

按需求选一个形态，取对应命令；后续小节的验证与初始化对所有形态通用。

| 形态 | 包含 | 命令（在 `$REPO` 执行） |
| --- | --- | --- |
| A. 仅运行期 | consumer 前后端 | `./deploy/deploy-stack.sh up --consumer-only` |
| B. 运行期 + 建模（推荐） | A + 建模工作台 | `./deploy/deploy-stack.sh up` |
| C. B + 解耦鉴权 | B + auth-server | 先做第 3.3 节配置，再 `./deploy/deploy-stack.sh up --with-auth` |
| D. B + 评测闭环 | B + 评测库 MySQL | `./deploy/deploy-stack.sh up --with-eval` |

C 与 D 可叠加：`up --with-auth --with-eval`。

自动化流水线入口：构建使用仓库根目录的 `./deploy/build.sh <version> [amd|arm]`，部署使用解压后的发布包内 `./deploy/deploy.sh`。该入口会直接调用各服务自己的脚本：consumer 使用 `consumer/build.sh` / `consumer/deploy/deploy.sh`，modeling 使用 `modeling/build.sh` / `modeling/deploy/deploy.sh`。`deploy-stack.sh` 仍保留为源码环境下带 `--with-eval` 等选项的手动编排入口。

## 2. 前置条件

- Linux / macOS，Docker ≥ 20.10，Docker Compose v2（`docker compose`）或 v1 ≥ 1.28（`docker-compose`；本方案使用 compose profiles，更旧的 v1 会失败，preflight 会检查）；
- 宿主端口 5055、8888、5100 空闲（形态 D 另需 3306）；
- 一个 OpenAI-compatible 的 LLM 端点（Base URL / 模型名 / API Key）。**没有也能部署**：建模工作台支持 `faux` 演示模式，consumer 的模型可以部署后在控制台配置；
- 目标机可访问 Docker Hub 或已配镜像源（首次构建需拉取 `python`、`node:22-slim`、`nginx`、`mysql:8` 等基础镜像）。

自动检查（**先跑这个**）：

```bash
cd $REPO
./deploy/deploy-stack.sh preflight
```

成功判据：输出全部为 `[OK]`（`[WARN]` 可继续；`[FAIL]` 按提示处理后重跑）。

## 3. 一键部署

### 3.1 形态 A / B / D：直接启动

```bash
cd $REPO
./deploy/deploy-stack.sh up            # 形态 B；A 加 --consumer-only，D 加 --with-eval
```

脚本行为（按序）：确保 `consumer/backend/.env` 存在（缺失时从 `.env.example` 复制）→ 构建并启动 consumer → 等待 backend/frontend 健康 → 探测 consumer 网络 → 构建并启动 modeling（及可选 eval-mysql）→ 等待健康 → 打印访问入口。

成功判据：最后输出 `[OK] 部署完成`，且第 7 节验证清单全部通过。

给建模工作台配真实 LLM（不配则默认 `faux` 演示模式，也可以部署后在页面配置中心配置）：

```bash
MODELING_LLM_BASE_URL=https://api.deepseek.com \
MODELING_LLM_MODEL=deepseek-chat \
MODELING_LLM_API_KEY=sk-xxx \
./deploy/deploy-stack.sh up
```

### 3.2 形态 D 附加说明（评测闭环）

`--with-eval` 会启动 `eval-mysql`，并要求显式设置 `EVAL_MYSQL_ROOT_PASSWORD`；脚本把 `EVAL_DB_URL` 接到它（密码不会使用默认值）。建模工具和 NL2SQL 服务共用该连接；每个项目以 Scene 区分数据库，可创建固定名称 `mv_shadow_<项目id>`，Excel 会直接写入该数据库供查询类评测用例使用。建库和导入前页面会展示隐藏密码后的连接供确认，也可仅为本次项目操作输入其他 URL；该值不会持久化或覆盖 `EVAL_DB_URL`。

查询类用例还需要一个数据服务（NL2SQL 桥接）供 consumer 侧查询评测库，这是一个宿主进程（暂无容器化），按需启动：

```bash
cd $REPO/modeling
export NL2SQL_TOKEN EVAL_DB_URL MODELING_LLM_BASE_URL MODELING_LLM_MODEL MODELING_LLM_API_KEY
npx tsx scripts/nl2sql-service.ts     # 监听 5600
```

仅做「业务问答类」用例评测时可跳过本步骤。详见 [modeling/docs/real-e2e-setup.md](../modeling/docs/real-e2e-setup.md)。

### 3.3 形态 C：启用解耦鉴权（opt-in）

1. 编辑 `consumer/backend/.env`，加入四行；管理令牌与管理员初始密码必须分别设置，禁止复用：

```bash
AUTH_MODE=remote
AUTH_SERVER_URL=http://auth-server:5056
AUTH_SERVER_ADMIN_TOKEN=<自定义共享密钥>
AUTH_BOOTSTRAP_ADMIN_PASSWORD=<首次启动使用的独立强密码>
```

2. 启动：

```bash
cd $REPO
./deploy/deploy-stack.sh up --with-auth
```

脚本会校验四行齐全（缺任意一行即 `[FAIL]` 中止并打印上述模板），并把配置注入 auth-server 容器；敏感值不会写入日志。auth-server 不暴露宿主端口，仅 backend 经容器网访问；首启使用 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 创建管理员 `admin` 并强制改密。自助注册默认开启，注册为一步直接建号，无邮箱验证码，无需任何 SMTP/邮件配置；不允许自行建号时设置 `AUTH_ENABLE_REGISTRATION=false`。

## 4. 首次初始化（部署后必做）

1. **consumer 改密**：打开 `http://localhost:8888`，用 `admin` 和配置的 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 登录，按提示修改初始密码；`users.json` 创建后可从环境中移除该初始密码。
2. **consumer 配模型**：控制台 → 配置中心 → 全局配置 → 模型池：添加 OpenAI-compatible 模型并「测试」通过、设为默认。（若已在 `backend/.env` 配了 `LLM_*` 可跳过。）
3. **建模工作台配置**（形态 B/C/D）：打开 `http://localhost:5100`，用 consumer 账号登录 → 右上角「⚙ 配置中心」：
   - **大模型**：填 Base URL / 模型名 / API Key，点「测试有效性」须通过；试玩填 `faux`。（compose 已注入 `MODELING_LLM_*` 时可跳过。）
   - **consumer 服务**：默认已指向容器网内 backend，点「测试连接」，须三项全绿（服务可达 / 认证通过 / 模型就绪——第三项依赖步骤 2）。
   - **评测数据**（形态 D）：评测库地址默认已注入，无需改动。
4. **验证**：执行第 7 节验证清单。

## 5. 手动分步部署（等价命令）

编排脚本不可用时（或需要拆步排障），以下命令与 `up` 等价：

```bash
# 1) consumer（工作目录 $REPO/consumer）
cd $REPO/consumer
cp -n backend/.env.example backend/.env || true
docker compose up -d --build
#   启用 auth 时（Admin Token 须以环境变量传给 auth-server 容器，与 backend/.env 中同值）：
#   AUTH_SERVER_ADMIN_TOKEN=<与 backend/.env 同值> \
#   AUTH_BOOTSTRAP_ADMIN_PASSWORD=<首次启动使用的独立强密码> \
#   docker compose --profile auth up -d --build
curl -fsS http://127.0.0.1:5055/health            # 预期 HTTP 200

# 2) 探测 consumer 网络名（modeling compose 需要）
export CONSUMER_NETWORK=$(docker network ls --format '{{.Name}}' | grep -E '(^|_|-)onto-agent-network$' | head -1)
echo $CONSUMER_NETWORK                            # 预期非空，形如 consumer_onto-agent-network

# 3) modeling（工作目录 $REPO/deploy）
cd $REPO/deploy
docker compose -p mw-modeling -f docker-compose.modeling.yml --project-directory . up -d --build
#   评测闭环：先从安全存储注入 EVAL_MYSQL_ROOT_PASSWORD 和 EVAL_DB_URL，再执行：
#     docker compose -p mw-modeling \
#     -f docker-compose.modeling.yml --project-directory . --profile eval up -d --build
curl -fsS http://127.0.0.1:5100/health            # 预期 HTTP 200
```

## 6. 配置参考

### 6.1 consumer（`consumer/backend/.env` + compose 层变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 未设置 | OpenAI-compatible 模型；也可部署后在控制台系统设置页配置 |
| `AUTH_MODE` | `local` | `remote` 时对接 auth-server（见 3.3） |
| `AUTH_SERVER_URL` / `AUTH_SERVER_ADMIN_TOKEN` | 空 | remote 鉴权必填 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 空 | 首次创建 auth-server 用户文件时必填；初始化后可移除 |
| compose：`TAG` | `latest` | 镜像 tag |
| compose：`BACKEND_PORT` / `FRONTEND_PORT` | `5055` / `8888` | 宿主端口映射 |

完整变量与发布包部署（离线 tar 包 + deploy/upgrade/rollback 脚本）见 [consumer/deploy/README.md](../consumer/deploy/README.md)。

### 6.2 modeling（compose 已接好默认值，均可用环境变量覆盖）

| 变量 | compose 默认 | 说明 |
| --- | --- | --- |
| `MODELING_LLM_BASE_URL` / `MODELING_LLM_MODEL` / `MODELING_LLM_API_KEY` | `faux` / `faux` / 空 | 建模 agent 的 LLM；`faux` 为无 Key 演示模式；页面配置中心优先于环境变量 |
| `MODELING_TASK_LLM_*` | 空 | 预留项，当前版本未生效（判分/摘要与建模均使用主模型） |
| `AUTH_ISSUER_URL` | `http://backend:5055` | 登录转发目标（= consumer），部署期固定、页面不可改 |
| `CONSUMER_API_URL` | `http://backend:5055` | 评测用 consumer 地址默认值（页面可覆盖） |
| `EVAL_DB_URL` | 空 | NL2SQL 查询与 modeling 建库/Excel 导入共用的 MySQL 连接；`--with-eval` 时脚本按显式 `EVAL_MYSQL_ROOT_PASSWORD` 生成 |
| `PORT` / `DATA_DIR` | `5100` / `/data`（卷 `modeling-data`） | 服务端口 / SQLite 数据目录 |
| compose：`MODELING_PORT` / `MODELING_TAG` | `5100` / `latest` | 宿主端口 / 镜像 tag |
| compose：`CONSUMER_NETWORK` | `consumer_onto-agent-network` | consumer 网络实际名（脚本自动探测注入） |

### 6.3 auth-server（compose 透传，前缀 `AUTH_`）

常用（compose 透传，在宿主 shell 设置即生效）：`AUTH_SERVER_ADMIN_TOKEN`（Admin API 共享密钥，须与 backend/.env 一致，编排脚本自动同步）、`AUTH_BOOTSTRAP_ADMIN_PASSWORD`（首次创建用户文件时使用的独立强密码）、`AUTH_ENABLE_REGISTRATION`（默认 `true`；设为 `false` 可关闭自助注册）。注册一步直接建号，无邮箱验证码，无需 SMTP。其余变量（如 `AUTH_ACCESS_TTL_SECONDS`）compose 未透传，需改 compose 或裸机运行才能覆盖；完整表见 [auth-server/README.md](../auth-server/README.md)。

## 7. 验证清单（机器可执行）

逐条执行，全部满足预期即部署成功：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5055/health   # 预期 200（consumer backend）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8888/         # 预期 200（控制台）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5100/health   # 预期 200（建模工作台；形态 A 跳过）
./deploy/deploy-stack.sh status                                           # 预期各服务 [OK] 可达、容器 Status 含 healthy
# 登录链路（预期 200，验证 modeling → consumer 转发；密码为改密后的值）：
curl -fsS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:5100/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"<改密后的密码>"}'
# 启用 --with-eval 时（预期 mysqld is alive；密码经临时环境变量传入，不进入命令参数）：
docker exec -e MYSQL_PWD="${EVAL_MYSQL_ROOT_PASSWORD:?请先设置评测库密码}" \
  mw-eval-mysql mysqladmin ping -uroot 2>/dev/null
```

## 8. 运维：升级 / 备份 / 停止

- **升级（源码部署）**：`git pull` 后重跑 `./deploy/deploy-stack.sh up <原参数>`——compose 会重建镜像，数据卷与 `.onto-runtime/` 保留。
- **升级（生产发布包，全栈）**：在仓库根目录执行 `./deploy/build.sh <version>` 构建离线发布包，包内 `deploy/deploy.sh` 会依次调用 consumer 与 modeling 的服务部署脚本，包含 backend / frontend / auth-server / modeling 镜像与健康门禁。consumer 单服务发布包见 [consumer/deploy/README.md](../consumer/deploy/README.md)。
- **备份**：
  ```bash
  # consumer 运行时（宿主目录，直接归档）
  tar -czf onto-runtime-$(date +%Y%m%d).tar.gz -C $REPO/consumer .onto-runtime
  # 建模项目数据（SQLite 卷）
  docker run --rm -v mw-modeling_modeling-data:/data -v "$PWD":/backup alpine \
    tar -czf /backup/modeling-data-$(date +%Y%m%d).tar.gz -C /data .
  # 评测库（可再生数据：Excel 重传即可重建，通常无需备份）
  # 鉴权数据（启用 auth 时）
  tar -czf onto-auth-$(date +%Y%m%d).tar.gz -C $REPO/consumer .onto-auth
  ```
- **停止**：`./deploy/deploy-stack.sh down`；连数据卷一起删（**不可恢复**）：`down --volumes`。

## 9. 故障排查

| 症状 | 检查命令 | 处理 |
| --- | --- | --- |
| preflight 报端口占用 | `lsof -iTCP:<port> -sTCP:LISTEN` | 停掉占用进程，或用 `BACKEND_PORT`/`FRONTEND_PORT`/`MODELING_PORT` 换端口后重跑 |
| backend 起不来 / 不健康 | `./deploy/deploy-stack.sh logs backend` | 常见为 `.env` 格式错误或模型端点不通；改后重跑 `up` |
| modeling 报「未找到 consumer 网络」 | `docker network ls \| grep onto-agent` | 先 `up --consumer-only`，或手动 `export CONSUMER_NETWORK=<实际网络名>` |
| 建模工作台登录 401 | `docker exec mw-modeling node -e "fetch('http://backend:5055/health').then(r=>console.log(r.status))"` | 预期打印 200；不通说明不在同一网络——用脚本重新 `up`；账号密码须是 consumer 的有效账号 |
| 建模评测报连不上 consumer | 建模页面 配置中心 → consumer 服务 → 测试连接 | 「模型就绪」红：先在 consumer 控制台配模型池；「认证」红：token 过期，重新登录 |
| 查询类评测用例全失败 | 确认 `--with-eval` 已启用 + NL2SQL 桥接已启动（3.2 节） | 在 consumer 控制台该影子工作区配置数据连接指向 NL2SQL 服务 |
| `docker compose` 命令不存在 | `docker-compose version` | 脚本自动兼容 v1 ≥ 1.28（profiles 所需）；更旧或两者都没有则安装 Compose v2 |
| 构建拉不到基础镜像 | `docker pull node:22-slim` | 配置镜像加速源，或在可联网机器构建后 `docker save/load` 导入 |

## 10. 已知边界

- 根部全栈发布包包含 backend / frontend / auth-server / modeling 镜像；`consumer/build.sh` 仍只构建 consumer 单服务发布包，适用于不部署建模工作台的场景。
- NL2SQL 桥接服务（`modeling/scripts/nl2sql-service.ts`）暂无容器化，作为宿主进程运行。
- modeling 的 `AUTH_VERIFY_MODE` 仅支持 `introspect`；`jwks` 为预留项未实现。
- 建模上传的文档正文存于 SQLite（卷 `mw-modeling_modeling-data`），不落宿主文件系统。
