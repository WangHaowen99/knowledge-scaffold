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
| onto-modeling | `modeling/onto-modeling/` | 5100 → 8100 | onto-modeling | auth-server / knowledge-hub | 引导式建模工作台，独立 compose 项目 |
| auth-server | `auth-server/` | 不暴露宿主端口（容器网内 5056） | onto-agent-auth-server | — | 必备鉴权服务，consumer 与 onto-modeling 共用 |
| knowledge-hub | `knowledge-hub/` | 不暴露宿主端口（容器网内 8200） | knowledge-hub | auth-server / onto-modeling | 知识包 Hub，独立 compose 项目（`knowledge-hub/deploy/docker-compose.knowledge-hub.yml`） |
| knowledge-evaluation | `eval-service/` | 5180 → 5180（供浏览器打开评测页面） | knowledge-evaluation | auth-server / knowledge-hub / consumer-backend | 独立评测服务，单实例，独立 compose 项目（`eval-service/deploy/docker-compose.eval-service.yml`） |

服务间调用链：`浏览器 → frontend(8888) → backend(5055)`；onto-modeling 共享 consumer 账号中心，经 consumer 网络访问知识包 Hub。知识包 Hub 与独立评测服务同样加入 consumer 网络，按服务名访问账号服务、Hub 与 consumer。

数据持久化位置：

| 数据 | 位置 | 备份方式见 |
| --- | --- | --- |
| consumer 运行时（知识包/工作区/会话/配置） | `consumer/.onto-runtime/`（宿主目录挂载） | 第 8 节 |
| 知识包 Hub（包归档、请求日志、项目最新资料） | docker 卷 `knowledge-hub_knowledge-hub-data` | [Hub README](../knowledge-hub/README.md#备份) |
| 鉴权数据 | `consumer/.onto-auth/`（宿主目录挂载） | 第 8 节 |

## 1. 部署形态选择

按需求选一个形态，取对应命令；后续小节的验证与初始化对所有形态通用。

| 形态 | 包含 | 命令（在 `$REPO` 执行） |
| --- | --- | --- |
| A. 仅运行期 | consumer + auth-server + JiuwenBox | `./deploy/deploy.sh --consumer-only` |
| B. 完整部署（推荐） | A + 知识包 Hub + Onto Modeling + 独立评测服务 | `./deploy/deploy.sh` |

`deploy/deploy.sh` 会自动识别源码树和解压后的发布包：源码树转入 `deploy-stack.sh` 构建 consumer 镜像，源码和发布包均依次部署 consumer、知识包 Hub、Onto Modeling 与独立评测服务。评测运行配置默认由评测部署脚本维护在 `eval-service/deploy/.env`（自动生成、权限 600、不入库），沿用 Hub 与 consumer 的评测凭据；设置 `EVAL_ENV_FILE` 时改用仓库外受保护的配置文件，文件不存在则在变更服务前报错。完整离线包包含 Hub 与评测镜像。自动化流水线构建入口仍是 `./deploy/build.sh <version> [amd|arm]`。Modeling Workbench 不在 `deploy/` 的构建和部署范围内，见第 10 节。

## 2. 前置条件

- Linux / macOS，Docker ≥ 20.10，Docker Compose v2（`docker compose`）或 v1 ≥ 1.28（`docker-compose`；本方案使用 compose profiles，更旧的 v1 会失败，preflight 会检查）；
- 宿主端口 5055、8888、5100、5180 空闲；
- 一个可供 admin 在部署后配置的 OpenAI-compatible LLM 端点；LLM 不再是部署前置条件；
- 目标机可访问 Docker Hub 或已配镜像源（首次构建需拉取 `python`、`node:22-slim`、`nginx` 等基础镜像）。

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

脚本会在 `MCP_TOKEN_SECRET` 或 `MODELING_SERVICE_TOKEN` 缺失时分别生成独立随机值并安全写回 `.env`，在缺少对应 tag 时构建固定版本 JiuwenBox 镜像，然后启动 consumer、auth-server、MCP 与 JiuwenBox，容器健康后依次部署知识包 Hub、Onto Modeling 与独立评测服务。`MODELING_SERVICE_TOKEN` 是 consumer 的入站凭据，供单独部署的 Modeling Workbench 对接。

### 3.2 鉴权说明

auth-server 已是必备组件，无需额外开关。脚本校验配置后把敏感值从 `consumer/backend/.env` 注入容器且不打印具体值；首启使用 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 创建管理员 `admin`，该密码可直接登录。自助注册默认开启，不允许自行建号时设置 `AUTH_ENABLE_REGISTRATION=false`。

## 4. 首次初始化（部署后必做）

1. **consumer 登录**：打开 `http://localhost:8888`，用 `admin` 和配置的 `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 登录；`users.json` 创建后可从环境中移除该引导密码。
2. **consumer 配模型**：控制台 → 配置中心 → 全局配置 → 模型池：添加 OpenAI-compatible 模型并「测试」通过、设为默认。（若已在 `backend/.env` 配了 `LLM_*` 可跳过。）
3. **验证**：执行第 7 节验证清单。

## 5. 手动分步部署（等价命令）

编排脚本不可用时（或需要拆步排障），以下命令与 `up` 等价：

```bash
# 1) consumer、auth-server、JiuwenBox
cd $REPO
./deploy/deploy.sh --consumer-only
curl -fsS http://127.0.0.1:5055/health

# 2) 知识包 Hub、Onto Modeling、独立评测服务（按此顺序）
./deploy/deploy.sh --knowledge-hub-only
./deploy/deploy.sh --onto-modeling-only
./deploy/deploy.sh --eval-only
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

### 6.2 auth-server（compose 透传，前缀 `AUTH_`）

常用（compose 透传，在宿主 shell 设置即生效）：`AUTH_SERVER_ADMIN_TOKEN`（Admin API 共享密钥，须与 backend/.env 一致，编排脚本自动同步）、`AUTH_BOOTSTRAP_ADMIN_PASSWORD`（首次创建用户文件时使用的独立强密码）、`AUTH_ENABLE_REGISTRATION`（默认 `true`；设为 `false` 可关闭自助注册）。注册一步直接建号，无邮箱验证码，无需 SMTP。其余变量（如 `AUTH_ACCESS_TTL_SECONDS`）compose 未透传，需改 compose 或裸机运行才能覆盖；完整表见 [auth-server/README.md](../auth-server/README.md)。

## 7. 验证清单（机器可执行）

逐条执行，全部满足预期即部署成功：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5055/health   # 预期 200（consumer backend）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8888/         # 预期 200（控制台）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5100/api/health  # 预期 200（Onto Modeling）
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5180/evaluation/api/health # 预期 200（独立评测服务）
./deploy/deploy-stack.sh status                                           # 预期各服务 [OK] 可达、容器 Status 含 healthy
```

## 8. 运维：升级 / 备份 / 停止

- **升级（源码部署）**：更新代码后重跑 `./deploy/deploy.sh`，compose 会重建镜像，数据卷保持不变。
- **升级（生产发布包，全栈）**：在仓库根目录执行 `./deploy/build.sh <version>`；发布包包含 consumer、知识包 Hub、Onto Modeling 与独立评测服务，包内 `deploy/deploy.sh` 依次部署这四个服务。
- **备份**：
  ```bash
  # consumer 运行时（宿主目录，直接归档）
  tar -czf onto-runtime-$(date +%Y%m%d).tar.gz -C $REPO/consumer .onto-runtime
  # 鉴权数据（启用 auth 时）
  tar -czf onto-auth-$(date +%Y%m%d).tar.gz -C $REPO/consumer .onto-auth
  ```
- **停止**：`./deploy/deploy-stack.sh down` 只停止 consumer；连 consumer 数据卷一起删（**不可恢复**）：`down --volumes`。

## 9. 故障排查

| 症状 | 检查命令 | 处理 |
| --- | --- | --- |
| preflight 报端口占用 | `lsof -iTCP:<port> -sTCP:LISTEN` | 停掉占用进程，或用 `BACKEND_PORT`/`FRONTEND_PORT` 换端口后重跑 |
| backend 起不来 / 不健康 | `./deploy/deploy-stack.sh logs backend` | 常见为 `.env` 格式错误或模型端点不通；改后重跑 `up` |
| `docker compose` 命令不存在 | `docker-compose version` | 脚本自动兼容 v1 ≥ 1.28（profiles 所需）；更旧或两者都没有则安装 Compose v2 |
| 构建拉不到基础镜像 | `docker pull node:22-slim` | 配置镜像加速源，或在可联网机器构建后 `docker save/load` 导入 |

## 10. 已知边界

- 根部全栈发布包包含 consumer（backend / frontend / auth-server / JiuwenBox）、知识包 Hub、Onto Modeling 与独立评测服务；`consumer/build.sh` 仍只构建 consumer 单服务发布包。
- Modeling Workbench 不在 `deploy/` 的构建和部署范围内：用 `bash modeling/build.sh <version> [amd|arm]` 单独打包，用 `modeling/deploy/` 下的脚本单独部署（`deploy/docker-compose.modeling.yml` 仅供其源码部署使用）。单独部署时 Workbench 须使用 consumer `backend/.env` 中的 `MODELING_SERVICE_TOKEN`。
- 已有的 Workbench 实例不受 `deploy/` 脚本影响；升级时脚本只读取它占用的宿主端口，避免 Onto Modeling 抢占。


## 知识包 Hub（独立部署）

分离安装、升级及旧版迁移统一部署四个必需服务：Consumer、Onto Modeling、知识包 Hub、独立评测服务。
必须提供 `--consumer-package`、`--onto-modeling-package`、`--knowledge-hub-package`、`--evaluation-package`，
缺少任一参数即退出；交互入口同样不能留空。
部署顺序为 Consumer → Hub → Onto Modeling → 评测。
使用与发布包同版本的安装脚本；完整离线包必须包含四项交付物，不以服务已经运行为由跳过。
`./deploy/build.sh` 生成完整离线包的同时，保留四个必需服务的独立发布包：`consumer/dist/onto-agent-*`、
`modeling/onto-modeling/dist/onto-modeling-*`、`knowledge-hub/dist/knowledge-hub-*`、`eval-service/dist/evaluation-*`
（同版本旧包会被覆盖）。Consumer 独立包与根目录 `dist/` 下的完整包同名，传给 `--consumer-package` 时应使用
`consumer/dist/` 下的文件。

Hub 不再写在 onto-modeling 的 compose 里，而是独立 compose 项目 `knowledge-hub/deploy/docker-compose.knowledge-hub.yml`（项目名、容器名 `knowledge-hub`，数据卷 `knowledge-hub_knowledge-hub-data`），由 `knowledge-hub/deploy/deploy.sh` 部署：加入 consumer 网络，onto-modeling 经 `http://knowledge-hub:8200` 访问，Hub 经 `auth-server` 校验令牌、经 `onto-modeling` 回调资料授权；不发布宿主端口，部署后在容器内做健康检查。

- `bash deploy/deploy.sh --knowledge-hub-only` 只部署 Hub，与其他 only 参数互斥，不包含评测；源码树从 `knowledge-hub/` 构建镜像，发布包使用包内 `knowledge-hub/` 子包。默认整栈在 Onto Modeling 之前部署 Hub；`--onto-modeling-only` 不再连带部署 Hub。
- 服务凭据：Hub 读写所在目录 `.env` 的 `HUB_SERVICE_TOKEN` 与 `HUB_EVALUATION_SERVICE_TOKEN`（源码为 `knowledge-hub/deploy/.env`），首次部署沿用 onto-modeling 已有的 `ONTO_HUB_SERVICE_TOKEN`，都没有时生成；部署 onto-modeling 前，脚本把后者对齐为 Hub 实际使用的值，评测服务部署时沿用 Hub 的评测凭据。值只写入权限 600 的 `.env`，不打印。源码部署的旧配置 `deploy/.env` 会在首次部署时迁移到新位置，确认后可删除旧文件。
- 离线包：`bash knowledge-hub/build.sh <version> [amd|arm]`；分离安装脚本通过 `--knowledge-hub-package`（交互脚本按提示输入）部署到 `INSTALL_ROOT/knowledge-hub`，详见[交互式部署与升级脚本说明](交互式部署与升级脚本说明.md)。
- 从 Hub 随 onto-modeling 部署的旧版本切换：重新部署 onto-modeling 时 `--remove-orphans` 会清理旧 Hub 容器；旧卷不自动迁移。

## 独立评测服务

`install-consumer-modeling.sh` 的 `--evaluation-package` 是必填参数；交互安装和旧版迁移同样必填。
升级统一更新四个必需服务，不触碰 Modeling Workbench，保留已有运行配置、数据卷及评测加密密钥。
保留原有容器健康检查；本次不增加依赖就绪 API 或逐层网络探测。

`bash deploy/deploy.sh --eval-only` 只部署 `eval-service/`，与其他 only 参数互斥；默认全栈 `bash deploy/deploy.sh` 包含评测服务，源码、离线首次安装和升级均在其他服务之后部署评测；`--onto-modeling-only`、`--knowledge-hub-only` 不包含评测服务。

- 交互式与非交互式分离安装、分离升级及旧版迁移均支持独立评测服务：交互式按提示输入发布包路径；非交互式使用 `--evaluation-package <发布包>`。发布包由 `eval-service/build.sh` 生成（`evaluation-<version>-linux-<arch>.tar.gz`），也接受完整离线包中的 `eval-service/` 目录，必须提供；评测在 Hub、Onto Modeling 之后部署并检查就绪。Consumer 已运行时仍须提供 Consumer 发布包，使用升级流程；不再提供省略 Consumer 的附加部署模式。发布包格式、凭据和升级配置保留规则见[部署与升级脚本说明](交互式部署与升级脚本说明.md)。
- 运行配置：默认由 `eval-service/deploy/deploy.sh` 维护本目录 `.env`（权限 600，不入库），保存凭据、显式配置以及选定的网络和 Auth 默认值。首次部署生成数据加密密钥 `EVAL_ENCRYPTION_KEY`；整栈入口从 Consumer 当前 Auth 配置读取地址、管理令牌并核对签发身份，再传给 Hub、Onto Modeling 和评测。评测的 `EVAL_AUTH_URL`、`EVAL_AUTH_ISSUER` 会显式进入容器；使用 `EVAL_ENV_FILE` 时外部文件保持不变。单独部署时网络、Auth URL 和 issuer 分别默认 `onto-auth-network`、`http://auth-server:5056`、`http://auth-server:5056`，网络仍须已存在；Hub、consumer 地址的默认值写在 `docker-compose.eval-service.yml`，`.env` 中的同名值优先。评测凭据沿用 Hub 的 `HUB_EVALUATION_SERVICE_TOKEN` 和 consumer 的 `EVALUATION_SERVICE_TOKEN`，两者分别由 Hub 部署脚本和 consumer 部署生成。评测完成通知发往 onto-modeling，凭据 `EVAL_MODELING_SERVICE_TOKEN` 沿用 onto-modeling `.env` 的 `ONTO_EVALUATION_SERVICE_TOKEN`（部署 onto-modeling 时缺失则生成）；找不到时只告警、不发通知，部署 onto-modeling 后重新部署评测即可。onto-modeling 经 `ONTO_WEB_EVAL_URL`（默认 `http://knowledge-evaluation:5180`）读取评测报告，须与评测同在 consumer 网络。评测容器与 Hub 一样必须加入 consumer 网络（`CONSUMER_NETWORK`，由根目录部署脚本探测注入并记入该 `.env`），探测不到时在生成配置前报错。
- Judge 模型配置 `EVAL_JUDGE_BASE_URL`、`EVAL_JUDGE_API_KEY`、`EVAL_JUDGE_MODEL`、`EVAL_JUDGE_CONFIG_REVISION` 四项必填、无法自动生成，已在 `docker-compose.eval-service.yml` 中声明（不含值）：部署时缺失只告警，写入该 `.env` 或进程环境后重新部署评测即可；未配置时无法开始评测。
- 加密密钥必须与数据卷一同保留：已有评测数据卷而 `.env` 缺少密钥时部署直接失败，不会生成新密钥；升级时若暂存目录没有评测配置，会沿用安装目录中的 `.env`。
- 设置 `EVAL_ENV_FILE` 时改用仓库外受保护的配置文件，脚本不改写该文件。
- 在本次改动之前已部署的整栈，Hub 和 consumer 还没有评测凭据：需要完整执行一次 `bash deploy/deploy.sh`，单独 `--eval-only` 会告警缺少凭据。

独立镜像、离线包及独立入口配置见 [评测部署说明](../eval-service/README.md)。
