# 部署说明

本目录包含 `onto-agent` 的发布、部署、升级、回滚脚本及说明。

## 本地开发

详细的本地开发指南和环境配置见根目录 [README.md](../README.md) 的「快速开始」章节。


## 源码部署

适用于开发、测试或 CI 场景，直接从源码构建镜像并启动容器，无需打包发布。

在项目根目录执行：

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

在仓库根目录执行：

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

- 部署前检查（Docker、架构匹配、端口占用）
- 运行目录初始化（自动创建所需目录，自动从 `backend/.env.example` 复制 `backend/.env`）
- 镜像导入
- 容器启动
- 部署后健康检查

部署成功后，通过系统设置页面配置 LLM、MindsDB 等对接参数即可。

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


## 运行目录

发布包保持与当前项目一致的挂载目录：

- `./.onto-runtime`（包含 `config/system_config.json`）
- `./backend/config`（只读旧配置迁移源）
- `./backend/logs`
- `./frontend/logs/nginx`
