# Onto Modeling 部署说明

离线交付包部署流程，对应方案第八节与 4.1.1 容量规划。

## 交付包内容

```
onto-modeling/
  images/                     # onto-modeling 与 auth-server 镜像 tar
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

### 对接评测服务

评测已移出 onto-modeling（ADR-0020），onto 不主动调用评测服务，也不再关联 Modeling 项目；
旧版的 `ONTO_WEB_MODELING_*` 与评测并发上限变量已不再读取（清单见项目 README），`.env` 里留着
也无影响，可以删掉。评测服务改为调用 onto 的三个契约入口，凭据是管理员签发的服务令牌：

1. 管理员 `POST /api/v1/admin/service-tokens` 签发令牌（形如 `onto_svc_…`，只在响应里出现一次），按需选范围：
   `releases.read`（发布列表、发布记录、包清单与按版本下载）、`receipts.write`（求解验证候选包与验证回执）、
   `feedback.write`（外部反馈）；可用 `project_ids` 限定项目、`expires_in_s` 设有效期，用完 `DELETE` 吊销。
2. 评测服务带 `Authorization: Bearer <服务令牌>` 调用 `/api/v1/contract/releases`、
   `/api/v1/projects/{id}/releases/{version}`（及 `/manifest`、`/download`）、`/api/v1/projects/{id}/solver/candidate`、
   `/api/v1/projects/{id}/solver/receipts`、`/api/v1/projects/{id}/feedback`。

令牌只存摘要（数据目录 `service-tokens.json`，0600），和项目的 MCP token 分开签发、分开存储。
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
- 建议主机配置 2–4G swap 或 zram 吸收峰值。
- 空闲会话默认 10 分钟回收（`ONTO_WEB_SESSION_IDLE_TTL`），回收后靠 resume
  续接模型上下文，用户无感；会话 transcript 持久化在数据卷 `/data/home/.claude/`。

## AVX 指令集要求（x86_64）

捆绑的 Claude Code CLI 是 Bun 编译的原生二进制，要求 CPU 暴露 AVX，否则子进程
SIGILL 崩溃。`preflight.sh` 会检查；未通过时按优先级整改：

1. **虚拟化层整改**：CPU 模型改 host-passthrough 或任一带 AVX2 的具体型号（零代码成本）。
2. **换 ARM 交付包**：aarch64 无 AVX 概念，无此限制。
3. **降级兼容包（最后手段）**：在目标机准备 Node ≥18 运行时和旧版 JS 交付的
   CLI（2.0.76，纯 JS 无 Bun/AVX 依赖），将其挂载进容器并设
   `ONTO_WEB_CLI_PATH=/path/to/cli.js`。注意：降级组合功能落后且冻结维护，
   必须在目标虚机实测关键路径（确认卡、hooks、子代理、会话恢复）后才能放行，
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
- 运维管理面在容器内 `127.0.0.1:8101`。compose 只发布业务端口，没有反代时在 `.env` 设
  `ONTO_WEB_OPS_ON_MAIN=true`，运维中心经业务端口访问；有反代时设 `ONTO_WEB_OPS_HOST=0.0.0.0`、只在内部网络
  暴露 8101，由反代把 `/api/v1/ops/` 转过去。
- 用户账号在 `onto-web-auth-data`，不在应用备份里；建议在主机侧对两个卷做定期快照。

## 升级与回滚

- 升级：解包新版本交付包，重复部署步骤；`docker compose up -d` 滚动替换容器，
  数据卷保留。
- 首次启动新版本时，存量项目会各自多出一个提交「chore: 停止跟踪项目元数据与工具状态」
  （元数据与工具状态不再进项目 git，回退版本不再回滚所有者），已迁移的项目不重复提交。
- 容器以 `init: true` 运行（镜像入口也是 tini）：应用不是 1 号进程，孤儿进程有人回收。
  `redeploy-app.sh` 沿用部署目录里的旧 compose，`init: true` 要等下一次完整部署才写进去，
  这期间由镜像里的 tini 兜底。
- 回滚：用旧版本交付包重复部署步骤即可（镜像 tag 随版本走，互不覆盖）。

## 接入既有账号中心（单点账号）

onto-modeling 默认自带独立 auth-server。若要与 consumer / modeling 栈共用同一
用户库（账号密码即刻通用），在 `.env` 里加两行后重跑 `./deploy.sh`：

```
ONTO_MODELING_AUTH_URL=http://auth-server:5056
ONTO_MODELING_EXTERNAL_AUTH_NETWORK=consumer_onto-agent-network
```

deploy.sh 会停用自带 auth-server、让应用容器加入 consumer 栈网络，直接复用其
`auth-server` 服务（issuer/audience 一致，token 互认）。切换回自带模式：删除这
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

## 从原 modeling 服务迁移数据

deploy.sh 部署完成后会自动检测 `mw-modeling` 容器（可用
`ONTO_MODELING_MIGRATE_FROM=<容器名>` 覆盖），存在即执行
`migrate-from-modeling.sh` 把项目/资料/知识包迁入本服务；迁移幂等，
已迁移的项目自动跳过，也可随时手动重跑该脚本。
