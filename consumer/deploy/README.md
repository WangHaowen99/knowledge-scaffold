# 部署说明

本目录包含 `onto-agent` 的发布、部署、升级、回滚脚本及说明。

## 本地开发

详细的本地开发指南和环境配置见 `consumer/` 应用目录 [README.md](../README.md) 的「快速开始」章节。


## 源码部署

适用于开发、测试或 CI 场景，直接从源码构建镜像并启动容器，无需打包发布。

在 `consumer/` 应用目录执行：

```bash
chmod +x ./run_docker.sh
./run_docker.sh [tag] [all|backend|frontend]
```

- `tag`：可选，用于标记镜像版本和容器名称，默认为 `latest`
- `all`（默认）：同时启动后端和前端
- `backend`：仅启动后端
- `frontend`：仅启动前端

示例：

```bash
./run_docker.sh              # 全栈启动，tag 默认为 latest
./run_docker.sh dev          # 全栈启动，tag 为 dev
./run_docker.sh dev backend  # 仅启动后端
```
## 发布入口

在 `consumer/` 应用目录执行：

```bash
chmod +x ./deploy/*.sh
./deploy/build_release.sh <版本号>
```

示例：

```bash
./deploy/build_release.sh v1.2.0
```

不指定架构参数时，脚本会自动检测当前机器架构（x86_64 → amd，aarch64/arm64 → arm），构建与当前机器相同架构的镜像。

如需指定架构，可追加参数：

```bash
./deploy/build_release.sh <版本号> [amd|arm]
```

- `amd`：构建 linux/amd64 镜像，适用于 x86_64 服务器
- `arm`：构建 linux/arm64 镜像，适用于 ARM 服务器（如鲲鹏、苹果 M 系列）

构建完成后，`dist/` 目录下生成发布包：

```text
dist/onto-agent-<版本号>-linux-<架构>.tar.gz
```

发布包内已包含所有部署、升级、回滚、重启脚本，无需额外文件。
发布包**不包含** `.onto-runtime`；部署和升级时会保留目标机器现有的 `.onto-runtime` 目录，如不存在则创建空目录。聊天会话、执行记录和 awaiting-input checkpoint 的 release 默认兜底目录也固定落在 `.onto-runtime/data/` 下。

## 首次部署

1. 将发布压缩包（如 `onto-agent-v1.2.0-linux-arm64.tar.gz`）拷贝到目标机器
2. 选择一个安装路径，例如 `/opt`，解压压缩包：

```bash
cd /opt
tar -xzf onto-agent-<版本号>-linux-<架构>.tar.gz
```

3. 进入安装目录，从示例创建 `backend/.env`，配置 `AUTH_SERVER_ADMIN_TOKEN`，再执行部署。用户数量不受配置限制，旧版的 `USER_MAX_COUNT` 和 `AUTH_USER_MAX_COUNT` 可删除：

```bash
cd /opt/onto-agent
cp backend/.env.example backend/.env
# 编辑 backend/.env
./deploy/deploy.sh
```

首次部署时，若 `backend/.env` 中未配置或配置为空 `MCP_TOKEN_SECRET`，部署脚本会生成独立随机值并写回该文件，不会打印密钥。已配置但不足 32 个字符时仍会中止部署，避免静默覆盖已有配置；升级和重启会继续使用已保存的值。

升级旧安装时，如果安装目录根部已有 `.env`，部署脚本只会保留其中的 `BACKEND_PORT`、`FRONTEND_PORT`、`MCP_SERVER_PORT` 三个宿主端口；其他变量不会传给 Compose。同名端口以根目录 `.env` 为准，新安装仍统一在 `backend/.env` 中维护配置。

部署脚本会自动完成：

- 部署前检查（Docker、架构、auth 配置、用户上限、MCP 7488/secret、Graph provider）
- 运行目录初始化（自动创建所需目录，自动从 `backend/.env.example` 复制 `backend/.env`）
- 镜像导入
- 容器启动（`backend`、`mcp`、`frontend`、`auth-server`）
- 部署后健康检查

部署成功后，通过系统设置页面配置 LLM、MindsDB 等对接参数即可。若需要对外提供 MCP 客户端接入，请在 `backend/.env` 中设置 `MCP_PUBLIC_BASE_URL` 为经过审批、可从客户端访问的 HTTPS 域名，例如 `https://mcp.example.com`。

## 升级

假设当前安装目录为 `/opt/onto-agent`（默认），从 v1.0.0 升级到 v1.1.0：

1. 将新版本发布包拷贝到目标机器并解压到任意目录（如 `/tmp`）：

```bash
cd /tmp
tar -xzf onto-agent-<版本号>-linux-<架构>.tar.gz
```

2. 执行升级脚本（默认安装目录为 `/opt/onto-agent`）：

```bash
/tmp/onto-agent/deploy/upgrade.sh
```

如果安装目录不是默认的，传入路径即可：

```bash
/tmp/onto-agent/deploy/upgrade.sh /data/onto-agent
```

`upgrade.sh` 会自动完成以下操作：

- **解压前备份**：保留覆盖前快照；正式一致性备份仍由 `deploy.sh` 在停止旧写入后创建
- **覆盖新文件**：将新版本的镜像、脚本等文件覆盖到安装目录
- **恢复保护文件**：从备份中恢复配置、数据和运行时文件，防止被覆盖
- **执行部署**：调用 `deploy.sh` 完成升级（镜像加载、迁移、容器更新、健康检查）

## 回滚

如需回滚到上一个成功版本，在安装目录下执行：

```bash
cd /opt/onto-agent
./deploy/rollback.sh
```

回滚脚本会自动完成：

- 先备份当前失败现场
- 保留当前 `.onto-runtime`、`.onto-auth` 与运行中生成的数据，只回退镜像/代码
- 如备份中包含旧镜像，则恢复旧镜像
- 重新启动上一版本
- 再次执行健康检查

## 重启服务

部署成功后，如需手动重启，在安装目录下执行：

```bash
cd /opt/onto-agent
./deploy/restart.sh
```


## 独立鉴权服务 auth-server（必备组件）

发布包内含 `onto-agent-auth-server` 镜像。remote 是唯一鉴权路径，auth-server 随
backend 一起部署、升级、回滚、重启，backend 依赖其健康（`service_healthy`）后才启动。

### 配置

安装目录下的 `backend/.env` 必填以下三项：

```bash
AUTH_SERVER_URL=http://auth-server:5056
AUTH_SERVER_ADMIN_TOKEN=<自定义共享密钥>
AUTH_BOOTSTRAP_ADMIN_PASSWORD=<首次启动使用的独立强密码>
```

三个敏感值不得互相复用；`AUTH_SERVER_ADMIN_TOKEN` 须与 auth-server 容器的同名环境变量一致。配置后执行
`./deploy/deploy.sh`（或已部署过则 `./deploy/restart.sh`）即可，compose 会随主服务
一起拉起 auth-server，无需额外参数。已有 `users.json` 的部署可以不再保留管理员初始密码。

auth-server **不发布宿主端口**，仅 backend 通过容器网络访问 `http://auth-server:5056`；
健康检查由镜像自带的 HEALTHCHECK 提供。可通过环境变量透传覆盖默认配置：
`AUTH_SERVER_ISSUER`、`AUTH_SERVER_ADMIN_TOKEN`（须与 `backend/.env` 一致）、
`AUTH_BOOTSTRAP_ADMIN_PASSWORD`（首次创建用户文件时使用）、
`AUTH_ENABLE_REGISTRATION`。注册默认开启并一步直接建号，无需邮箱验证码，也无需配置任何
SMTP / 邮件相关变量；不允许自行建号时显式设置 `AUTH_ENABLE_REGISTRATION=false`。

### 数据目录与备份

auth-server 数据挂载在安装目录的 `./.onto-auth/`（含 `users.json`、`keys/` 签名私钥）。
该目录已纳入：

- `backup.sh`：打包为 `onto-auth.tar.gz`，auth 镜像同时 `docker save`
- `upgrade.sh`：升级覆盖文件时列入保护清单，不会被新发布包覆盖
- `rollback.sh`：执行 code-only rollback，不会自动覆盖 `.onto-runtime`、`.onto-auth` 或会话数据；如需恢复备份，必须显式人工操作

### 存量用户、workspace 与本体数据源迁移

`deploy.sh` 固定按以下顺序执行：迁移前基础预检（持久化格式校验交给 migration dry-run）→ migration dry-run → 停止旧容器写入（不立即删除容器）→ 一致性备份与旧容器 `/app/data/{chats,executions}` 导出 → apply migration → 删除已停止的旧容器 → 严格预检 → 启动新容器。

- 只有 `.onto-runtime/users.json` 时，原样复制密码哈希到 `.onto-auth/users.json`，目标权限为 `0600`。
- 只有 auth users 时直接使用。
- 两份同时存在时做字段级语义比较；缺失 `token_version` 按 `1`。任一字段不同立即失败，差异报告不会泄露密码哈希。
- 若旧 release 仍把 fallback 会话/执行数据写在 backend 容器可写层 `/app/data/chats` 或 `/app/data/executions`，升级时会先从已停止但尚未删除的旧容器导出这些目录，再合并到 `.onto-runtime/data/chats` 与 `.onto-runtime/data/executions`。若目标持久目录已存在同名且内容不同的文件，迁移直接失败，要求人工核对。
- 无 owner 的存量 workspace 继续作为公共空间，报告列出但不自动私有化。
- 未知 owner/member、重复成员或未完成用户删除 reconciliation 会阻止启动，避免同名账号继承旧权限。
- `connection_ref` 与旧 `ontology-bindings.json` 会展开为每个 ontology 独立的 `direct_provider` 配置。apply 前会先校验全部 workspace，校验失败不修改原数据；apply 仍按 forward-only 执行，但在首次写入前会为实际存在的 `ontology-data-sources.json`、`data-connections.json`、`ontology-bindings.json` 生成同目录 `*.pre-direct-v2.bak` 原始字节副本。副本权限固定为 `0600`，重试不会覆盖首次副本，路径同时记录在迁移报告和完成标记中。每个 workspace 的新格式写入并回读校验成功后才删除旧 `data-connections.json` 与 `ontology-bindings.json`；清理失败会阻止启动，并可在修复文件权限后继续重试。
- `*.pre-direct-v2.bak` 可能包含明文凭据，只作为人工排查和恢复材料；应用运行时与迁移 runner 不会自动读取这些文件，`rollback.sh` 也不会把它们作为单文件恢复输入。人工恢复前必须先停止写入并核对同一 workspace 下各副本的一致性。部署级回滚仍使用 `backup.sh` 生成的整包备份，不受本迁移副本影响。
- 本体数据源迁移是后端镜像中的独立一次性模块，只在 `backend/migrations/runner.py` 的通用注册表中登记。部署流程和 Flask 启动共用该 runner；运行时 store 不包含兼容逻辑。兼容窗口结束后只需删除注册项、独立迁移模块及对应测试说明，`app.py`、`deploy.sh` 和 `migrate.sh` 无需修改。

可在停机前读取检查：

```bash
./deploy/migrate.sh <旧版本> <新版本> --dry-run
```

上线新版本前必须先执行上述 ACL dry-run，并检查报告中的：

```json
{
  "workspace_acl": {
    "clean": true,
    "findings": []
  }
}
```

若 `clean=false`，迁移保持严格阻断，且只输出提示，不会自动修改 `workspace.json`，也没有跳过 ACL 校验的放行参数。常见存量问题按以下方式处理：

- `unknown_member`：该成员仍应保留访问权时，由管理员在认证中心创建 findings 中完全同名的账号；不应保留时，从对应 workspace 的 members 中移除。
- `unknown_owner`：由管理员在认证中心创建 findings 中完全同名的账号，或通过 workspace API 将 owner 转移给已验证账号。
- `pending_user_deletion_reconciliation`：通过用户删除 reconciliation 管理接口完成或重试清理，不能直接复用同名账号。
- `duplicate_member`、`invalid_member`、`invalid_owner`：修正对应 ACL 字段格式或重复项。

优先在旧版本仍运行时通过配置中心或 workspace API 修复，避免绕过服务端并发保护直接编辑文件。每次处理后重新执行 dry-run，直到 `workspace_acl.clean=true` 再开始升级。这里“创建对应账号”是指管理员确认该历史用户名确实代表一个仍需访问空间的人后，在认证目录创建完全同名账号；迁移脚本本身不会代替管理员建号。

正式 apply 只允许在确认停写后执行：

```bash
./deploy/migrate.sh <旧版本> <新版本> --confirm-writes-stopped
```

用户与 workspace 报告写入 `deploy/state/migration-report.json`，后端通用迁移报告写入 `deploy/state/backend-migration-report.json`；本体数据源副本可从对应 workspace 结果的 `backup_paths` 查看。手工执行 `migrate.sh` 前需确保当前发布包的 backend 镜像已加载；正常 `deploy.sh` 会自动加载。旧 HMAC access token 升级后不再接受，用户需重新登录；JWT `sub` 仍等于 username，因此 session 路径无需迁移。

## 运行目录

发布包保持与当前项目一致的挂载目录：

- `./.onto-runtime`（包含 `config/system_config.json`）
- `./backend/logs`
- `./frontend/logs/nginx`
- `./.onto-auth`（始终创建，auth-server 用户数据）

## MCP 服务

发布部署默认启动 `onto-agent-mcp` 容器，复用后端镜像和 `.onto-runtime`：

发布包首次部署时，`backend/.env` 中缺失或为空的 `MCP_TOKEN_SECRET` 会由部署脚本自动生成独立随机值（至少 32 个字符）并以受限文件权限保存；直接使用其他启动方式时仍需自行配置。已填写的值必须至少 32 个字符，部署脚本不会覆盖。Token 明文仅在创建响应中返回一次，运行目录中只保存哈希；请妥善保管。

- Flask 管理接口：`/agentic/mcp/*`
- MCP endpoint 按 `MCP_TRANSPORT` 暴露相应协议；默认 `both` 同时提供：
  - `streamable-http`：`http://<host>:${MCP_SERVER_PORT:-7488}/{mcp_endpoint_id}/search/mcp`、`/{mcp_endpoint_id}/chat/mcp`
  - `sse`：`http://<host>:${MCP_SERVER_PORT:-7488}/{mcp_endpoint_id}/search/sse`、`/{mcp_endpoint_id}/chat/sse`
- 健康检查：`http://127.0.0.1:7488/health`

`mcp_endpoint_id` 在启动迁移中写入各工作空间的 `workspace.json`，之后保持不变。完整客户端 URL 从管理页面或 `GET /agentic/mcp/runtime/status` 获取；旧固定路由不再提供。`MCP_SERVER_HOST` 在 Compose 内部由服务覆盖：`backend` 容器使用 `mcp` 探测独立 MCP 容器，`mcp` 容器使用 `0.0.0.0` 监听。外部客户端展示地址不要依赖该字段，应使用 `MCP_PUBLIC_BASE_URL`。
