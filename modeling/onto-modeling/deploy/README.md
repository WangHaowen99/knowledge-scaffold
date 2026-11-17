# Onto Modeling 部署说明

离线交付包部署流程，对应方案第八节与 4.1.1 容量规划。

## 交付包内容

```
onto-modeling/
  images/                     # 镜像 tar：onto-modeling、auth-server、连接器网关
  images.env                  # 镜像 tag 清单（deploy.sh 自动读取）
  docker-compose.release.yml
  deploy.sh                   # 预检 → 加载镜像 → 拉起
  preflight.sh                # 可单独运行的部署预检
  .env.example                # 环境变量说明（复制为 .env 后填写）
  README.md
```

## 部署步骤

```bash
tar -xzf onto-modeling-<version>-linux-<arch>.tar.gz
cd onto-modeling
./deploy.sh          # 首次运行会生成 .env 并提示填写模型网关配置
vi .env              # 填 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
./deploy.sh          # 再次运行完成部署
```

访问 `http://<部署机地址>:5100`（端口可用 `ONTO_WEB_PORT` 覆盖）。

首次运行时 deploy.sh 在 `.env` 里生成四项，已有的值不动：初始管理员密码 `AUTH_BOOTSTRAP_ADMIN_PASSWORD`（登录名 admin）、
auth-server 管理令牌 `ONTO_WEB_AUTH_ADMIN_TOKEN`（auth-server 与后端读同一个值，运维中心「用户与审计」靠它），
以及下一节的 `ONTO_HUB_SERVICE_TOKEN`、`ONTO_EVALUATION_SERVICE_TOKEN`。

### 对接知识包 Hub 与评测服务

发布存到知识包 Hub，版本号由 Hub 分配；评测由独立评测服务做。两者各自独立部署（仓库 `knowledge-hub/`、
`eval-service/`），和本服务同在 consumer 网络上，按服务名 `knowledge-hub`、`knowledge-evaluation` 访问。
整栈部署（仓库根 `deploy/deploy.sh` 或安装脚本）按 Consumer → Hub → Onto Modeling → 评测的顺序部署，
凭据自动对齐。单独部署本包时连不到它们，发布、已发布版本和评测入口不可用。

| 变量 | 作用 | 取值 |
| --- | --- | --- |
| `ONTO_HUB_SERVICE_TOKEN` | 访问 Hub 的服务凭据 | 等于 Hub 的 `HUB_SERVICE_TOKEN`。deploy.sh 缺省时生成；整栈部署先部署 Hub，再改成 Hub 实际使用的值 |
| `ONTO_EVALUATION_SERVICE_TOKEN` | 校验评测完成通知 `POST /api/internal/evaluation/notifications` | deploy.sh 缺省时生成，评测部署沿用它；不能等于 Hub 凭据，留空则拒绝通知 |
| `ONTO_WEB_EVAL_URL` | 后端访问评测服务 | compose 默认 `http://knowledge-evaluation:5180` |
| `ONTO_WEB_EVAL_PUBLIC_URL` | 浏览器打开评测页面的地址 | 空 = 与工作台同主机的 `http://<主机>:5180/evaluation/`；评测在别的主机、端口或走 HTTPS 时填完整地址，改后重新部署 |

单独升级本包时保留原 `.env`：凭据换了值，Hub 调用返回 401，评测通知被拒。
旧版的 `ONTO_WEB_MODELING_*` 与评测并发上限变量已不再读取（清单见项目 README），`.env` 里留着也无影响，可以删掉。

升级到这一版首次启动时会做一次性迁移：被对象引用的评测上传表迁进各项目 `resource/excel/` 并提交，
随后删除评测状态与数据目录下的评测数据源配置（含连接密码），详见项目 README。

升级到带插件运行时的版本（ADR 0024）首次启动时：镜像里的工具箱作为 core 包导入数据卷的 `packs/`，
每个存量项目写一份 `.onto/packs.lock` 并提交一次（主线版本号加 1），按它在数据卷的 `assembled/` 下装配，
项目的 `.claude` 改为指向装配目录的相对链接。`assembled/` 可由锁文件随时重建，`packs/` 是包仓库，
需要随数据卷一起备份。原来的「上传资产包」改为插件上传（管理员），没有清单的资产包当作 core 新版本，
静态检查通过后发布到 stable，各项目在下一条消息前切换。

## 容量规划（方案 4.1.1）

- 每个活跃建模会话 = 一个 CLI 子进程，均值约 400MB、峰值 500MB+。
- 8G 主机：保持默认 `ONTO_WEB_MAX_SESSIONS=12`，约支撑 12–15 并发活跃会话、
  30–50 名在线用户（突发式交互节奏）。
- 几十用户同时建模为目标时建议 16G 主机，并按比例调高 `ONTO_WEB_MAX_SESSIONS`
  与 compose 里的 `ONTO_WEB_MEM_LIMIT`。
- 自主构建的每次运行占一个会话（运行中不被驱逐、不被空闲回收），同时运行数由 `ONTO_WEB_AUTOBUILD_MAX_RUNS`
  （默认 2）限制，超出的排队；会话池上限要给陪伴建模的用户留出余量。
- 建议主机配置 2–4G swap 或 zram 吸收峰值。
- 空闲会话默认 10 分钟回收（`ONTO_WEB_SESSION_IDLE_TTL`），回收后靠 resume
  续接模型上下文，用户无感；会话 transcript 持久化在数据卷 `/data/home/.claude/`。
- 运行中的会话连续 30 分钟没有任何模型或工具响应（`ONTO_WEB_SESSION_STALL_TIMEOUT`，
  单位秒，0 关闭）即判定执行超时：终止引擎、释放槽位，对话里给出提示，用户可以直接发消息
  继续。等待确认卡的时间不计入。模型调用失败重试期间也没有响应，调小阈值前先确认模型网关
  的单次超时与重试次数。

## 子代理同步执行（CLI 后台任务开关）

平台在每个 Claude 引擎会话的 CLI 子进程中注入环境变量
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`，子代理和 Shell 命令一律同步执行。原因：CLI
默认把子代理放到后台运行，而平台以主轮结束判定会话空闲，后台运行的子代理会被误示为完成，
还可能随空闲回收被静默终止。

- 该变量由平台自动注入，无需手工配置。不要在容器内 `/data/home/.claude/settings.json`
  的 `env` 中设置它：CLI 启动后会读取那里的值，覆盖平台注入的值。
- 平台还会注入 `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1` 与
  `CLAUDE_CODE_BG_TASKS_REPORT_RUNNING=1`，让 CLI 上报自身的会话状态：后台工作未结束时，
  会话保持"运行中"，不会被误判为空闲、提前通知或被回收。这两个变量同样不要在上述
  `settings.json` 中覆盖。
- 命令超过本次调用的超时（默认 2 分钟，模型可以延长，最长 10 分钟）会直接失败，
  不再转入后台。
- 镜像内的 `claude-agent-sdk` 没有锁定版本，打包的 CLI 随构建时间变化。每次部署和升级后
  执行以下两项检查：

  1. 打包的 CLI 仍识别该变量，输出大于 0 即正常：

     ```bash
     docker exec onto-modeling python3 -c "import claude_agent_sdk, pathlib; cli = pathlib.Path(claude_agent_sdk.__file__).parent / '_bundled' / 'claude'; print(cli.read_bytes().count(b'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'))"
     ```

     输出为 0 说明该版本 CLI 已不识别此变量，应暂停升级并反馈研发。使用降级兼容包
     （`ONTO_WEB_CLI_PATH`）时本项不适用，按下文降级包的关键路径实测。
  2. 用户级配置没有覆盖该变量，没有输出即正常：

     ```bash
     docker exec onto-modeling sh -c 'grep -n CLAUDE_CODE_DISABLE_BACKGROUND_TASKS /data/home/.claude/settings.json 2>/dev/null'
     ```

## AVX 指令集要求（x86_64）

捆绑的 Claude Code CLI 是 Bun 编译的原生二进制，要求 CPU 暴露 AVX，否则子进程
SIGILL 崩溃。`preflight.sh` 会检查；未通过时按优先级整改：

1. **虚拟化层整改**：CPU 模型改 host-passthrough 或任一带 AVX2 的具体型号（零代码成本）。
2. **换 ARM 交付包**：aarch64 无 AVX 概念，无此限制。
3. **降级兼容包（最后手段）**：在目标机准备 Node ≥18 运行时和旧版 JS 交付的
   CLI（2.0.76，纯 JS 无 Bun/AVX 依赖），将其挂载进容器并设
   `ONTO_WEB_CLI_PATH=/path/to/cli.js`。注意：降级组合功能落后且冻结维护，
   必须在目标虚机实测关键路径（确认卡、hooks、子代理（含同步执行）、会话恢复）后才能放行，
   界面无"兼容模式"标识前不建议长期使用。
4. 以上均不可行则明确不支持该环境。

## 数据与备份

- 业务数据全部在 docker 卷 `onto-web-data`（项目 workspace、会话历史、设置）
  与 `onto-web-auth-data`（用户账号）。
- 应用备份：运维中心可手动备份，另按 `ONTO_WEB_BACKUP_INTERVAL_HOURS`（默认 24 小时）定时备份，
  服务启动时距上次备份已超过间隔会先补一份；保留最近 3 份。备份默认写在数据卷内的
  `/data/onto-web/backups`，与数据同卷，卷坏了一起丢。建议在 compose 的 override 里给
  onto-modeling 另挂一个卷（例如挂到 `/backups`），并在 `.env` 设 `ONTO_WEB_BACKUP_DIR=/backups`。
  范围与恢复方法见上级目录 `README.md`「数据备份」。
- 个人 Key 主密钥不进备份：请在 `.env` 设 `ONTO_WEB_MODEL_SECRET_KEY`（至少 32 字节，没设时启动告警）。
  没设时它是数据卷里自动生成的 `/data/onto-web/secrets/.model-secret-key`，需单独保管，或设
  `ONTO_WEB_KEY_BACKUP_DIR` 指到另一个卷，每次备份自动单独拷一份；异机恢复缺了它，已保存的个人 Key 无法解密。
- 密钥目录 `/data/onto-web/secrets/`（0700）放管理员模型 Key、个人 Key 密文、MCP token 库与服务令牌库；
  升级后第一次启动会把数据卷根目录下的老文件搬进去。agent 以镜像里的 `onto-agent` 用户运行，读不到它。
- 运维管理面在容器内 `127.0.0.1:8101`，compose 只发布业务端口。compose 默认设 `ONTO_WEB_OPS_ON_MAIN=true`：
  没有反代的单端口部署，运维中心经业务端口访问（运维接口仍要登录令牌与 ops 角色）。有反代时在 `.env` 设
  `ONTO_WEB_OPS_ON_MAIN=false`、`ONTO_WEB_OPS_HOST=0.0.0.0`，只在内部网络暴露 8101，由反代把
  `/api/v1/ops/` 转过去，业务端口上的运维接口返回 404。
- 用户账号在 `onto-web-auth-data`，不在应用备份里；建议在主机侧对两个卷做定期快照。

## 升级与回滚

- 升级：解包新版本交付包，重复部署步骤；`docker compose up -d` 滚动替换容器，
  数据卷保留。
- 首次启动新版本时，存量项目会各自多出一个提交「chore: 停止跟踪项目元数据与工具状态」
  （元数据与工具状态不再进项目 git，回退版本不再回滚所有者），已迁移的项目不重复提交。
- 容器以 `init: true` 运行（镜像入口也是 tini）：应用不是 1 号进程，孤儿进程有人回收。
  `redeploy-app.sh` 沿用部署目录里的旧 compose，`init: true` 要等下一次完整部署才写进去，
  这期间由镜像里的 tini 兜底。
- 升级后按「子代理同步执行（CLI 后台任务开关）」一节执行两项检查。
- 回滚：用旧版本交付包重复部署步骤即可（镜像 tag 随版本走，互不覆盖）。

## 接入既有账号中心（单点账号）

onto-modeling 默认自带独立 auth-server。若要与 consumer / modeling 栈共用同一
用户库（账号密码即刻通用），在 `.env` 里加两行后重跑 `./deploy.sh`：

```
ONTO_MODELING_AUTH_URL=http://auth-server:5056
ONTO_MODELING_EXTERNAL_AUTH_NETWORK=consumer_onto-agent-network
```

deploy.sh 会停用自带 auth-server、让应用容器加入 consumer 栈网络，直接复用其
`auth-server` 服务（issuer/audience 一致，token 互认）。用户管理要外部 auth-server 的管理令牌：把它的
`AUTH_SERVER_ADMIN_TOKEN` 填进 `ONTO_WEB_AUTH_ADMIN_TOKEN`。切换回自带模式：删除这
两行重跑 deploy.sh 即可（自带 auth 数据卷始终保留）。需要 docker compose v2.24+。

## 脚本运行验证的沙箱（jiuwenbox）

代码生成的脚本必须跑通才能提交（`/ontology-codegen` Step 4.6）。验证在 consumer 的
`onto-agent-jiuwenbox` 沙箱里执行，脚本随请求送进一次性沙箱（禁止出网、仅 `/tmp` 可写）。

jiuwenbox 的管理 socket 只挂给脚本验证网关 `onto-modeling-sandbox-gateway`（compose 服务
`sandbox-gateway`），不挂进 onto-modeling 容器：建模 agent 在 onto-modeling 容器里能执行任意命令，
拿到 socket 就能自己定沙箱策略。网关与 onto-modeling 同一镜像，以非 root、只读根文件系统、无
capability 运行，不读 `.env`、不挂数据卷、不发布端口；它只按固定策略运行脚本，onto-modeling 经
`ONTO_WEB_SANDBOX_GATEWAY_URL=http://sandbox-gateway:8110` 调用它。

- 整栈部署（根目录 `deploy/deploy.sh`、`install-consumer-modeling*.sh`）会探测 consumer 的
  socket 卷，写入 `.env`：`ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=<卷名>`；deploy.sh 据此生成
  `docker-compose.jiuwenbox.yml`，把卷只读挂给网关。`redeploy-app.sh` 沿用该文件并一起替换网关。
- 单独部署、或探测不到 consumer 的沙箱时不挂载，网关报 jiuwenbox 不可用，脚本验证在 onto-modeling
  容器内执行。
- 管理员可在「配置中心 → 沙箱」查看是否已连接、沙箱内 Python 版本与包列表，并重新检测。

## 连接器网关（资料转文本）

用户上传的 PDF（文字版）、Word、Excel 在后台转成 Markdown 文本版，供资料分析和助手读（ADR-0073）。转换不在
onto-modeling 容器里做：上传的文件不可信，解析可能卡死、吃满内存，解析库也不该出现在助手所在的容器里。

- 服务 `connector-gateway`（容器 `onto-modeling-connector-gateway`）用**单独的镜像**
  `onto-modeling-connector-gateway:<版本>`（同一个 Dockerfile 的 `connector-gateway` 目标），离线包里多一个
  镜像 tar，`deploy.sh` 一起加载；`redeploy-app.sh` 一起重建、替换（旧部署目录的 compose 没有这个服务时跳过）。
- 只接 `internal: true` 的 `connector` 网络（和 onto-modeling 互通），没有出口；不读 `.env`、不发布端口；
  非 root、只读根文件系统、无 capability，内存 1.5G、最多 64 个进程。
- 每个转换一个子进程：120 秒超时、768 MiB 内存、16 MiB 输出上限，同时最多 2 个；
  可用 `ONTO_CONNECTOR_JOB_TIMEOUT_S`、`ONTO_CONNECTOR_JOB_MEMORY_MB`、`ONTO_CONNECTOR_MAX_CONCURRENT` 调（网关容器的环境变量）。
- onto-modeling 经 `ONTO_WEB_CONNECTOR_GATEWAY_URL=http://connector-gateway:8120` 调它，每次带一张内核签的一次性
  票据；网关从 `http://onto-modeling:8100/api/v1/connector/issuer-key` 取公钥验签。
- 网关停了不丢活：资料标「等待转换」，网关恢复后 30 秒内自动补转；升级后已有项目里还没有文本版的资料在后台补转一次。
- 健康状态在运维中心「健康与依赖」的「连接器网关」一行和启动自检报告里。

### 从带系统证据采集的版本升级

系统逆向建模包和数据库、代码仓库、程序逆向连接器已去掉。`deploy.sh` 用 `--remove-orphans` 清掉旧的
`onto-modeling-rea-sidecar` 容器；以下内容不自动删除，确认不再需要后手工清理：

- 卷 `onto-web_connector-secrets`（数据源凭据密文）和 `onto-web_onto-web-rea-scratch`；
- 部署目录下的 `secrets/connector-master.key`，以及 `.env` 里的 `ONTO_CONNECTOR_MASTER_KEY_FILE`、
  `ONTO_WEB_EVIDENCE_MAX_ARTIFACT_MB`、`ONTO_WEB_EVIDENCE_SNAPSHOT_GIT_MB`；
- 数据卷里的 `evidence/` 目录（数据源登记、采集任务、目标程序和 REA 原始结果）。项目工作区里已提交的
  `resource/systems/` 快照留在版本历史里，不再读取。

## 从原 modeling 服务迁移数据

deploy.sh 部署完成后会自动检测 `mw-modeling` 容器（可用
`ONTO_MODELING_MIGRATE_FROM=<容器名>` 覆盖），存在即执行
`migrate-from-modeling.sh` 把项目/资料/知识包迁入本服务；迁移幂等，
已迁移的项目自动跳过，也可随时手动重跑该脚本。
Workbench 数据库处于 WAL 模式，脚本在 `mw-modeling` 容器内用 `VACUUM INTO` 导出一致性快照再迁移，
因此需要该容器在运行中。迁移失败不影响 Onto Modeling 本身，deploy.sh 会打印 `[WARN]` 和重跑命令。
