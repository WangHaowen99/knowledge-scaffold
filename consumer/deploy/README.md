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
发布包**不包含** `.onto-runtime`；部署和升级时会保留目标机器现有的 `.onto-runtime` 目录，如不存在则创建空目录。

## 首次部署

1. 将发布压缩包（如 `onto-agent-v1.2.0-linux-arm64.tar.gz`）拷贝到目标机器
2. 选择一个安装路径，例如 `/opt`，解压压缩包：

```bash
cd /opt
tar -xzf onto-agent-<版本号>-linux-<架构>.tar.gz
```

3. 进入安装目录，执行部署：

```bash
cd /opt/onto-agent
./deploy/deploy.sh
```

部署脚本会自动完成：

- 部署前检查（Docker、架构匹配、Flask/MCP/前端端口占用）
- 运行目录初始化（自动创建所需目录，自动从 `backend/.env.example` 复制 `backend/.env`）
- 镜像导入
- 容器启动（`backend`、`mcp`、`frontend`）
- 部署后健康检查

部署成功后，通过系统设置页面配置 LLM、MindsDB 等对接参数即可。若需要对外提供 MCP 客户端接入，请在 `backend/.env` 中设置 `MCP_PUBLIC_BASE_URL` 为服务器 IP 或域名，例如 `http://192.0.2.20:7488`。

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

- **解压前备份**：将当前配置、业务数据、日志和 `.onto-runtime` 备份到 `deploy/backups/pre-extract-<timestamp>/`
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
- 恢复上一次成功版本的配置、数据、日志和 `.onto-runtime`
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

安装目录下的 `backend/.env` 必填以下两项：

```bash
AUTH_SERVER_URL=http://auth-server:5056
AUTH_SERVER_ADMIN_TOKEN=<自定义共享密钥>
```

`AUTH_SERVER_ADMIN_TOKEN` 须与 auth-server 容器的同名环境变量一致。配置后执行
`./deploy/deploy.sh`（或已部署过则 `./deploy/restart.sh`）即可，compose 会随主服务
一起拉起 auth-server，无需额外参数。

auth-server **不发布宿主端口**，仅 backend 通过容器网络访问 `http://auth-server:5056`；
健康检查由镜像自带的 HEALTHCHECK 提供。可通过环境变量透传覆盖默认配置：
`AUTH_SERVER_ISSUER`、`AUTH_SERVER_ADMIN_TOKEN`（须与 `backend/.env` 一致）、
`AUTH_ENABLE_REGISTRATION`。开启注册后一步直接建号，无需邮箱验证码，也无需配置任何
SMTP / 邮件相关变量。

### 数据目录与备份

auth-server 数据挂载在安装目录的 `./.onto-auth/`（含 `users.json`、`keys/` 签名私钥）。
该目录已纳入：

- `backup.sh`：打包为 `onto-auth.tar.gz`，auth 镜像同时 `docker save`
- `upgrade.sh`：升级覆盖文件时列入保护清单，不会被新发布包覆盖
- `rollback.sh`：回滚时随镜像一并恢复（备份不含该目录则跳过）

### 存量用户迁移导入

首次部署时，`migrate.sh` 会检测：若 `.onto-auth/users.json` 不存在
且 `.onto-runtime/users.json`（consumer 内嵌认证的存量用户）存在，则自动复制导入
（0600 权限）。用户密码哈希自包含，导入后无需重设密码。`.onto-auth/users.json`
已存在时跳过，不会覆盖。

> **升级提示**：从旧的内置认证（原 `AUTH_MODE=local`）部署升级前，务必先执行
> `migrate.sh` 将 `.onto-runtime/users.json` 导入到 `.onto-auth/`；且内置 HMAC 认证
> 回退已移除，升级后在线用户会被强制重新登录（与 ADR consequences 一致）。

## 运行目录

发布包保持与当前项目一致的挂载目录：

- `./.onto-runtime`（包含 `config/system_config.json`）
- `./backend/config`（只读旧配置迁移源）
- `./backend/logs`
- `./frontend/logs/nginx`
- `./.onto-auth`（始终创建，auth-server 用户数据）

## MCP 服务

发布部署会额外启动 `onto-agent-mcp` 容器，复用后端镜像和 `.onto-runtime`：

- Flask 管理接口：`/agentic/mcp/*`
- MCP endpoint 按 `MCP_TRANSPORT` 只暴露一种协议：
  - `streamable-http`：`http://<host>:${MCP_SERVER_PORT:-7488}/search/mcp`、`/chat/mcp`
  - `sse`：`http://<host>:${MCP_SERVER_PORT:-7488}/search/sse`、`/chat/sse`
- 健康检查：`http://127.0.0.1:7488/health`

`MCP_SERVER_HOST` 在 Compose 内部由服务覆盖：`backend` 容器使用 `mcp` 探测独立 MCP 容器，`mcp` 容器使用 `0.0.0.0` 监听。外部客户端展示地址不要依赖该字段，应使用 `MCP_PUBLIC_BASE_URL`。
