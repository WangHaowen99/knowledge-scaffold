# Onto Modeling 部署说明

离线交付包部署流程，对应方案第八节与 4.1.1 容量规划。

## 交付包内容

```
onto-modeling/
  images/                     # onto-modeling 与 auth-server 镜像 tar
  images.env                  # 镜像 tag 清单（deploy.sh 自动读取）
  docker-compose.release.yml
  deploy.sh                   # 预检 → 加载镜像 → 拉起
  configure-modeling-association.sh  # 自动配置导入项目的 MCP 关联
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

### Modeling MCP 关联与远端清理

整栈部署继续使用原有共用认证和全局 Modeling 地址。独立部署 Onto 发布包时，
若同机 Modeling 已通过项目部署脚本生成服务令牌，Onto 部署脚本自动读取该令牌及通信地址，
将 `ONTO_WEB_MODELING_ASSOCIATION_URL/TOKEN` 写入本机 `.env`，无需手工填写。
重复部署原样保留完整的关联地址和令牌；只有一项时停止并提示配置不完整。
已有全局 Modeling 地址时继续使用原用户鉴权，不注入本机服务令牌。
不会改变登录鉴权、网络连接或全局评测地址。
`.env` 含运行凭据，不可提交到 Git 或打入发布包。

独立部署 Onto 时，若同机已有运行中的 Modeling，脚本自动使用其已发布 HTTP 端口；
Docker Desktop 使用宿主网关域名，Linux Docker 使用宿主 bridge 网关，无需添加 `extra_hosts`。
只有 Onto、没有 Modeling 服务的部署无法提供 Modeling MCP；脚本会提示并保留独立部署能力。
自定义跨机部署仍需由运维提供关联地址和令牌，无法从本机自动发现远端服务。

专用关联通道只接受服务凭据和匹配的 Onto 项目归属。管理员授权仅限当前项目的
MCP 管理接口，不授予 Modeling 全局管理员权限、不修改成员列表。管理员代项目签发
Token 时使用项目所有者作为访问主体，签发及吊销记录保留实际管理员身份；运行时
继续校验原有项目成员权限、Token scope 和吊销状态。

如果 Onto Modeling 与核心 Modeling 共用认证中心，配置：

```env
ONTO_WEB_MODELING_URL=http://mw-modeling:8100
```

建议同时配置远端项目清理凭证，保证 Onto 项目创建失败或删除后，核心 Modeling
中的关联项目可以自动清理：

```env
ONTO_WEB_MODELING_CLEANUP_TOKEN=<随机高强度值>
ONTO_WEB_MODELING_CLEANUP_INTERVAL=30
```

核心 Modeling 侧配置同一个值：

```env
MODELING_MCP_CLEANUP_TOKEN=<与 Onto 相同的值>
```

该凭证只允许 Onto 后端与 Modeling 后端使用，不能写入前端、日志或普通用户请求。
未配置时不影响正常项目创建和 MCP 使用，但远端清理失败会进入 Onto 的持久化补偿
任务，可在管理员接口中查看和重试。清理任务不保存用户 Bearer Token。

## 容量规划（方案 4.1.1）

- 每个活跃建模会话 = 一个 CLI 子进程，均值约 400MB、峰值 500MB+。
- 8G 主机：保持默认 `ONTO_WEB_MAX_SESSIONS=12`，约支撑 12–15 并发活跃会话、
  30–50 名在线用户（突发式交互节奏）。
- 几十用户同时建模为目标时建议 16G 主机，并按比例调高 `ONTO_WEB_MAX_SESSIONS`
  与 compose 里的 `ONTO_WEB_MEM_LIMIT`。
- 建议主机配置 2–4G swap 或 zram 吸收峰值。
- 数据评测每轮在评测专用线程池里执行，`ONTO_WEB_EVAL_MAX_CONCURRENT_ROUNDS`
  （默认 16）限制同时执行的轮数，超出的轮次排队并在状态里显示位次；评测不占用
  上传、git、建模发消息共用的默认线程池。每轮在 modeling 侧按用例并发调用
  consumer，调高上限前先确认 consumer 与模型网关的承载。
- 空闲会话默认 10 分钟回收（`ONTO_WEB_SESSION_IDLE_TTL`），回收后靠 resume
  续接模型上下文，用户无感；会话 transcript 持久化在数据卷 `/data/home/.claude/`。

## 子代理同步执行（CLI 后台任务开关）

平台在每个 Claude 引擎会话的 CLI 子进程中注入环境变量
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`，子代理和 Shell 命令一律同步执行。原因：CLI
默认把子代理放到后台运行，而平台以主轮结束判定会话空闲，后台运行的子代理会被误示为完成，
还可能随空闲回收被静默终止。

- 该变量由平台自动注入，无需手工配置。不要在容器内 `/data/home/.claude/settings.json`
  的 `env` 中设置它：那里的值会覆盖平台注入的值。
- 超过 Bash 默认超时（2 分钟）的命令会直接超时失败，不再转入后台。
- 镜像内的 `claude-agent-sdk` 没有锁定版本，打包的 CLI 随构建时间变化。每次部署和升级后
  都要验证开关仍然生效：先在任一 Claude 引擎项目里发一条消息让会话启动，再执行

  ```bash
  docker exec onto-modeling sh -c 'grep -a -l "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1" /proc/[0-9]*/environ 2>/dev/null'
  ```

  有输出即生效。没有输出时，先检查上面的 `settings.json`，再确认镜像版本包含该机制。

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
- 运维中心提供手动备份（保留最近 3 份）；建议在主机侧对两个卷做定期快照。

## 升级与回滚

- 升级：解包新版本交付包，重复部署步骤；`docker compose up -d` 滚动替换容器，
  数据卷保留。
- 升级后按「子代理同步执行（CLI 后台任务开关）」一节验证开关仍然生效。
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
