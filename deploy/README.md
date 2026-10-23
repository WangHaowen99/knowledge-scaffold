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
| modeling | `modeling/` | 8100 → 5100 | mw-modeling | modeling-agent / consumer / auth-server | Workbench 静态页面与确定性 core |
| modeling-agent | `modeling/modeling-agent/` | 仅容器网 5110 | mw-modeling-agent | modeling core / LLM | 新 Workbench 的 openJiuwen Agent 运行时 |
| auth-server | `auth-server/` | 不暴露宿主端口（容器网内 5056） | onto-agent-auth-server | — | 必备鉴权服务，consumer 与 modeling 共用 |
| eval-mysql（可选） | — | 3306 → 3306 | mysql:8 | — | 建模评测库（Excel 数据自动落库供查询类用例） |

服务间调用链：`浏览器 → frontend(8888) → backend(5055)`；`浏览器 → modeling(8100) → modeling-agent(5110)`；modeling 登录直连 auth-server，评测调用 consumer。consumer 与 modeling 容器共享 Docker 网络；独立评测使用单独 Compose 网络，通过运行配置中的可达地址连接依赖。

数据持久化位置：

| 数据 | 位置 | 备份方式见 |
| --- | --- | --- |
| consumer 运行时（知识包/工作区/会话/配置） | `consumer/.onto-runtime/`（宿主目录挂载） | 第 8 节 |
| 建模项目（SQLite） | docker 卷 `mw-modeling_modeling-data` | 第 8 节 |
| Agent 运行与 checkpoint | docker 卷 `mw-modeling_modeling-agent-data` | 第 8 节 |
| 评测库 | docker 卷 `mw-modeling_eval-mysql-data` | 第 8 节 |
| 鉴权数据 | `consumer/.onto-auth/`（宿主目录挂载） | 第 8 节 |

## 1. 部署形态选择

按需求选一个形态，取对应命令；后续小节的验证与初始化对所有形态通用。

| 形态 | 包含 | 命令（在 `$REPO` 执行） |
| --- | --- | --- |
| A. 仅运行期 | consumer + auth-server + JiuwenBox | `./deploy/deploy.sh --consumer-only` |
| B. 完整工作台（推荐） | A + modeling core + modeling-agent | `./deploy/deploy.sh` |
| C. B + 评测闭环 | B + 评测库 MySQL / NL2SQL | `./deploy/deploy-stack.sh up --with-eval` |

`deploy/deploy.sh` 会自动识别源码树和解压后的发布包：源码树转入 `deploy-stack.sh` 构建镜像，源码和发布包均依次部署 consumer、Modeling Workbench、Onto Modeling 与独立评测服务。默认部署前须设置 `EVAL_ENV_FILE`，指向仓库外受保护的评测运行配置文件；缺失时在变更服务前报错。完整离线包包含评测镜像。自动化流水线构建入口仍是 `./deploy/build.sh <version> [amd|arm]`。

## 2. 前置条件

- Linux / macOS，Docker ≥ 20.10，Docker Compose v2（`docker compose`）或 v1 ≥ 1.28（`docker-compose`；本方案使用 compose profiles，更旧的 v1 会失败，preflight 会检查）；
- 宿主端口 5055、8888、8100 空闲（形态 C 另需 3306）；
- 一个可供 admin 在部署后配置的 OpenAI-compatible LLM 端点；LLM 不再是部署前置条件；
- 目标机可访问 Docker Hub 或已配镜像源（首次构建需拉取 `python`、`node:22-slim`、`nginx`、`mysql:8` 等基础镜像）。

自动检查（**先跑这个**）：

```bash
cd $REPO
./deploy/deploy-stack.sh preflight
```

成功判据：输出全部为 `[OK]`（`[WARN]` 可继续；`[FAIL]` 按提示处理后重跑）。

## 3. 一键部署

### 3.1 默认完整部署

```bash
cd $REPO
cp -n consumer/backend/.env.example consumer/backend/.env
# 编辑 consumer/backend/.env，至少填写下方 3 项
./deploy/deploy.sh
```

必填配置：

```dotenv
AUTH_SERVER_URL=http://auth-server:5056
AUTH_SERVER_ADMIN_TOKEN=<至少 16 个字符的独立共享密钥>
AUTH_BOOTSTRAP_ADMIN_PASSWORD=<首次部署使用的独立强密码>
```

脚本会在 `MCP_TOKEN_SECRET` 或 `MODELING_SERVICE_TOKEN` 缺失时分别生成独立随机值并安全写回 `.env`，在缺少对应 tag 时构建固定版本 JiuwenBox 镜像，然后启动 consumer、auth-server、MCP 与 JiuwenBox。之后探测网络和 socket 卷，再构建并启动 `mw-modeling-agent` 与 `mw-modeling`；所有容器健康且 `/workbench.html` 返回成功后才报告部署完成。

### 3.2 形态 C 附加说明（评测闭环）

`--with-eval` 会启动 `eval-mysql`，并要求显式设置 `EVAL_MYSQL_ROOT_PASSWORD`；脚本把 `EVAL_DB_URL` 接到它（密码不会使用默认值）。建模工具和 NL2SQL 服务共用该连接；每个项目以 Scene 区分数据库，可创建固定名称 `mv_shadow_<项目id>`，Excel 会直接写入该数据库供查询类评测用例使用。建库和导入前页面会展示隐藏密码后的连接供确认，也可仅为本次项目操作输入其他 URL；该值不会持久化或覆盖 `EVAL_DB_URL`。

查询类用例还需要 NL2SQL 数据服务供 consumer 查询评测库。`--with-eval` 会启动
`modeling-nl2sql`，并把工作台的数据源默认地址设为容器内地址
`http://modeling-nl2sql:5600`；它与 modeling 共用同一个 `EVAL_DB_URL`。

不使用 compose 时也可作为宿主进程启动：

```bash
cd $REPO/modeling
export NL2SQL_TOKEN EVAL_DB_URL MODELING_LLM_BASE_URL MODELING_LLM_MODEL MODELING_LLM_API_KEY
npx tsx scripts/nl2sql-service.ts     # 监听 5600
```

仅做「业务问答类」用例评测时可跳过本步骤。详见 [modeling/docs/real-e2e-setup.md](../modeling/docs/real-e2e-setup.md)。

### 3.3 鉴权说明

auth-server 已是必备组件，无需额外开关。脚本校验配置后把敏感值从 `consumer/backend/.env` 注入容器且不打印具体值；首启使用 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 创建管理员 `admin`，该密码可直接登录。自助注册默认开启，不允许自行建号时设置 `AUTH_ENABLE_REGISTRATION=false`。

## 4. 首次初始化（部署后必做）

1. **consumer 登录**：打开 `http://localhost:8888`，用 `admin` 和配置的 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 登录；`users.json` 创建后可从环境中移除该引导密码。
2. **consumer 配模型**：控制台 → 配置中心 → 全局配置 → 模型池：添加 OpenAI-compatible 模型并「测试」通过、设为默认。（若已在 `backend/.env` 配了 `LLM_*` 可跳过。）
3. **配置并进入新建模工作台**（形态 B/C）：打开 `http://localhost:8100/workbench.html#/create`，先用 `admin` 登录，在「配置中心 → 大模型 → 平台默认」填写 Base URL、模型名和 API Key，测试通过后保存。该配置会立即成为所有未设置个人覆盖账号的 core 与 Agent 统一默认，新任务无需重启容器即可使用。
   - **consumer 服务**：默认已指向容器网内 backend，点「测试连接」，须三项全绿（服务可达 / 认证通过 / 模型就绪——第三项依赖步骤 2）。
   - **评测数据**（形态 C）：评测库地址默认已注入，无需改动。
4. **验证**：执行第 7 节验证清单。

## 5. 手动分步部署（等价命令）

编排脚本不可用时（或需要拆步排障），以下命令与 `up` 等价：

```bash
# 1) consumer、auth-server、JiuwenBox
cd $REPO
./deploy/deploy.sh --consumer-only
curl -fsS http://127.0.0.1:5055/health

# 2) Modeling Workbench；自动探测 consumer 网络和 JiuwenBox socket 卷
./modeling/deploy/deploy.sh
#   评测闭环改用：
#     EVAL_MYSQL_ROOT_PASSWORD=<强随机值> \
#       ./deploy/deploy-stack.sh up --modeling-only --with-eval
curl -fsS http://127.0.0.1:8100/workbench.html
```

## 6. 配置参考

### 6.1 consumer（`consumer/backend/.env` + compose 层变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 未设置 | OpenAI-compatible 模型；也可部署后在控制台系统设置页配置 |
| `AUTH_SERVER_URL` / `AUTH_SERVER_ADMIN_TOKEN` | 地址有示例 / Token 空 | auth-server 必填 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 空 | 首次创建 auth-server 用户文件时必填；初始化后可移除 |
| compose：`TAG` | `latest` | 镜像 tag |
| compose：`BACKEND_PORT` / `FRONTEND_PORT` | `5055` / `8888` | 宿主端口映射 |

完整变量与发布包部署（离线 tar 包 + deploy/upgrade/rollback 脚本）见 [consumer/deploy/README.md](../consumer/deploy/README.md)。

### 6.2 modeling（从 `consumer/backend/.env` 读取，shell 环境变量优先）

| 变量 | compose 默认 | 说明 |
| --- | --- | --- |
| `MODELING_LLM_BASE_URL` / `MODELING_LLM_MODEL` | 空 | 可选部署兜底；推荐由 admin 登录 Workbench 后配置平台默认 |
| `MODELING_LLM_API_KEY` | 空 | 可选部署兜底密钥；admin 页面保存的 Key 会加密存储且不回显 |
| `MODELING_LLM_MAX_TOKENS` | `128000` | 单次模型输出 token 上限；与上下文窗口独立 |
| `MODELING_LLM_CONTEXT_WINDOW` | `1000000` | 输入+输出总上下文 token 窗口；按所选模型 API 实际上限填写 |
| `MODELING_AGENT_PIP_INDEX_URL` | 清华 PyPI 镜像 | 构建 modeling-agent 时使用的 Python 包索引；内网可设为企业镜像 |
| `MODELING_AGENT_PIP_TIMEOUT` / `MODELING_AGENT_PIP_RETRIES` | `120` / `10` | modeling-agent 依赖下载的单次读取超时秒数与重试次数 |
| `MODELING_SERVICE_TOKEN` | 自动生成 | modeling core、Agent 与 consumer 的服务间认证令牌；至少 32 字符，不是管理员 Token |
| `MODELING_TASK_LLM_*` | 空 | 无用户上下文的后台流程部署兜底；认证用户可在配置中心单独配置后台任务模型，留空则使用个人主模型 |
| `AUTH_ISSUER_URL` | `http://auth-server:5056` | modeling 登录与 JWT 验签的 auth-server 地址 |
| `CONSUMER_API_URL` | `http://backend:5055` | 评测用 consumer 地址默认值（页面可覆盖） |
| `EVAL_DB_URL` | 空 | NL2SQL 查询与 modeling 建库/Excel 导入共用的 MySQL 连接；`--with-eval` 时脚本按显式 `EVAL_MYSQL_ROOT_PASSWORD` 生成 |
| `PORT` / `DATA_DIR` | `5100` / `/data`（卷 `modeling-data`） | 服务端口 / SQLite 数据目录 |
| compose：`MODELING_PORT` / `MODELING_TAG` | `8100` / `latest` | 宿主端口 / 镜像 tag |
| compose：`CONSUMER_NETWORK` | `consumer_onto-agent-network` | consumer 网络实际名（脚本自动探测注入） |

源码部署时可在 `consumer/backend/.env` 设置上述 `MODELING_AGENT_PIP_*` 变量。制作发布包时，构建发生在部署之前，应通过 shell 环境变量传入：

```bash
MODELING_AGENT_PIP_INDEX_URL=https://pypi.example.com/simple \
MODELING_AGENT_PIP_TIMEOUT=120 \
MODELING_AGENT_PIP_RETRIES=10 \
./deploy/build.sh v1.2.0
```

### 6.3 auth-server（compose 透传，前缀 `AUTH_`）

常用（compose 透传，在宿主 shell 设置即生效）：`AUTH_SERVER_ADMIN_TOKEN`（Admin API 共享密钥，须与 backend/.env 一致，编排脚本自动同步）、`AUTH_BOOTSTRAP_ADMIN_PASSWORD`（首次创建用户文件时使用的独立强密码）、`AUTH_ENABLE_REGISTRATION`（默认 `true`；设为 `false` 可关闭自助注册）。注册一步直接建号，无邮箱验证码，无需 SMTP。其余变量（如 `AUTH_ACCESS_TTL_SECONDS`）compose 未透传，需改 compose 或裸机运行才能覆盖；完整表见 [auth-server/README.md](../auth-server/README.md)。

## 7. 验证清单（机器可执行）

逐条执行，全部满足预期即部署成功：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5055/health   # 预期 200（consumer backend）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8888/         # 预期 200（控制台）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/health   # 预期 200（建模工作台；形态 A 跳过）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/workbench.html # 预期 200（新 Workbench）
docker inspect --format '{{.State.Health.Status}}' mw-modeling-agent           # 预期 healthy
./deploy/deploy-stack.sh status                                           # 预期各服务 [OK] 可达、容器 Status 含 healthy
# 登录链路（预期 200，验证 modeling → consumer 转发；密码为改密后的值）：
curl -fsS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8100/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"<改密后的密码>"}'
# 启用 --with-eval 时（预期 mysqld is alive；密码经临时环境变量传入，不进入命令参数）：
docker exec -e MYSQL_PWD="${EVAL_MYSQL_ROOT_PASSWORD:?请先设置评测库密码}" \
  mw-eval-mysql mysqladmin ping -uroot 2>/dev/null
```

## 8. 运维：升级 / 备份 / 停止

- **升级（源码部署）**：更新代码后重跑 `./deploy/deploy.sh`，compose 会重建 core 与 Agent 镜像，数据卷保持不变。
- **升级（生产发布包，全栈）**：在仓库根目录执行 `./deploy/build.sh <version>`；发布包会包含 `mw-modeling` 与 `mw-modeling-agent` 两个镜像，包内 `deploy/deploy.sh` 依次部署 consumer 与 Workbench 并执行双健康门禁。
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
| modeling 报「未找到 JiuwenBox socket 卷」 | `docker volume ls \| grep onto-jiuwenbox-sock` | 先用根部署脚本启动 consumer/JiuwenBox，或显式设置 `JIUWENBOX_SOCKET_VOLUME` |
| modeling-agent 报模型未配置 | `./deploy/deploy-stack.sh logs modeling-agent` | 使用 admin 登录 Workbench，在「配置中心 → 大模型 → 平台默认」配置并测试主模型；无需重启容器 |
| 构建 modeling-agent 时访问 `files.pythonhosted.org` 超时 | 查看 Docker 构建日志中的 `ReadTimeoutError` | 更新到包含镜像配置的版本后重跑；企业网络可在 `backend/.env` 设置 `MODELING_AGENT_PIP_INDEX_URL=<内网 PyPI simple 地址>` |
| 建模工作台登录 401 | `docker exec mw-modeling node -e "fetch('http://backend:5055/health').then(r=>console.log(r.status))"` | 预期打印 200；不通说明不在同一网络——用脚本重新 `up`；账号密码须是 consumer 的有效账号 |
| 建模评测报连不上 consumer | 建模页面 配置中心 → consumer 服务 → 测试连接 | 「模型就绪」红：先在 consumer 控制台配模型池；「认证」红：token 过期，重新登录 |
| 查询类评测用例全失败 | 确认 `--with-eval` 已启用 + NL2SQL 桥接已启动（3.2 节） | 在 consumer 控制台该影子工作区配置数据连接指向 NL2SQL 服务 |
| `docker compose` 命令不存在 | `docker-compose version` | 脚本自动兼容 v1 ≥ 1.28（profiles 所需）；更旧或两者都没有则安装 Compose v2 |
| 构建拉不到基础镜像 | `docker pull node:22-slim` | 配置镜像加速源，或在可联网机器构建后 `docker save/load` 导入 |

## 10. 已知边界

- 根部全栈发布包包含 backend / frontend / auth-server / JiuwenBox / modeling core / modeling-agent 镜像；`consumer/build.sh` 仍只构建 consumer 单服务发布包。
- NL2SQL 桥接服务随 modeling 的 `eval` profile 容器化运行；非 compose 部署可继续使用宿主进程入口。
- modeling-agent 仅通过容器网络访问，不发布宿主 5110 端口；浏览器统一经 modeling core 的同源代理访问。
- 建模上传的文档正文存于 SQLite（卷 `mw-modeling_modeling-data`），不落宿主文件系统。


## 独立评测服务

`bash deploy/deploy.sh --eval-only` 只部署 `eval-service/`，与其他 only 参数互斥；默认全栈 `bash deploy/deploy.sh` 包含评测服务，源码、离线首次安装和升级均在其他服务之后部署评测；`--onto-modeling-only` 不包含评测服务。需要在进程环境设置 `EVAL_ENV_FILE` 指向仓库外受保护的运行配置，并提供账号、Hub、consumer 和可选数据库依赖。独立镜像、离线包及同源网关配置见 [评测部署说明](../eval-service/README.md)。
