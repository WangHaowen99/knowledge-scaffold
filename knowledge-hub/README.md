# Knowledge Package Hub

Hub 按 ontology_id（manifest.yaml.id）保存不可变知识包版本和最新共享资料。用户授权集合覆盖全部版本；服务身份另外限制发布类型。建模和评测使用同一身份契约，消费端现有 ZIP 协议保持不变。

不迁移存量数据，不兼容旧包字段或项目资料接口。资料不依赖建模项目或权限回调，项目删除不级联删除 Hub 内容。

## 运行

```bash
cd knowledge-hub/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python run.py
```

配置通过运行环境注入，示例见 [.env.example](.env.example)：

- `HUB_DATA_DIR`：独立本地持久化卷，单实例跨进程写锁。
- `HUB_AUTH_URL`：统一账号服务固定地址，Hub 请求其 `/userinfo` 验证签名、issuer、audience、类型及撤销状态。拒绝重定向，不持久化用户令牌。
- `HUB_AUTH_ISSUER`：用户身份使用的稳定身份命名空间，未设置时等于 HUB_AUTH_URL。运行后不要随意改变，否则用户身份键会变化。
- `HUB_SERVICE_TOKEN`：与建模 `ONTO_HUB_SERVICE_TOKEN` 对应，是建模访问包 API 和本体共享资料的统一服务身份。沿用部署脚本已有的自动生成逻辑，不按开发阶段另设凭据。
- 评测、消费分别使用 `HUB_EVALUATION_SERVICE_TOKEN`、`HUB_CONSUMER_SERVICE_TOKEN`，两者与建模凭据必须不同。评测凭据由部署脚本缺失时生成，独立评测服务部署时沿用；消费服务未接入可不配置。
- HUB_AUTH_DIRECTORY_TOKEN：现有账号服务 /admin/users 的目录凭据，仅新增授权时需要。由受控环境注入；不配置时新增授权返回 503，其他包操作不受影响。该凭据具有账号管理权限，应限制 Hub 运行环境的访问。
- 包上传默认 256 MiB，解压总量 1 GiB、单文件 256 MiB、成员数 10000；pending 默认保留 7 天。已占版本不会回收，已提交包不按 pending TTL 清理。

建模服务通过 `ONTO_HUB_URL`、`ONTO_HUB_SERVICE_TOKEN` 访问 Hub，缺少统一服务身份时失败关闭。接入外部账号服务时同时设置 Hub 的可达地址及稳定 issuer 命名空间，不仅设置建模的认证地址。生产凭据由受控运行环境注入，不写入仓库。

## 容器部署

Hub 是独立 compose 项目（[`deploy/docker-compose.knowledge-hub.yml`](deploy/docker-compose.knowledge-hub.yml)，部署脚本 [`deploy/deploy.sh`](deploy/deploy.sh)；项目名与容器名均为 `knowledge-hub`，数据卷 `knowledge-hub_knowledge-hub-data`），与 onto-modeling 分开部署、升级和重建，不发布宿主端口。它加入与账号中心、onto-modeling 共享的 Docker 网络（整栈即 consumer 网络），onto-modeling 经 `http://knowledge-hub:8200` 访问。

- 源码：`bash deploy/deploy.sh --knowledge-hub-only`（仓库根目录）只部署 Hub（自动探测 consumer 网络，从源码构建镜像）；默认整栈在 onto-modeling 之前部署 Hub，`--onto-modeling-only` 不触碰 Hub。
- 镜像源：构建时 Debian 软件源和 PyPI 默认都走华为云镜像。可用 `KNOWLEDGE_HUB_APT_MIRROR`（主机名，`deb.debian.org` 即官方源）和 `KNOWLEDGE_HUB_PIP_INDEX_URL`（或 `PIP_INDEX_URL`）覆盖，源码部署和离线包构建都生效，例如 `KNOWLEDGE_HUB_PIP_INDEX_URL=https://pypi.example.com/simple bash knowledge-hub/build.sh <version>`。基础镜像 `python:3.11-slim-bookworm` 仍从 Docker Hub 拉取，需要加速时在 Docker 守护进程中配置 registry mirror。
- 离线包：`bash knowledge-hub/build.sh <version> [amd|arm]` 生成 `knowledge-hub/dist/knowledge-hub-<version>-linux-<arch>.tar.gz`（镜像、compose、`deploy.sh`）。根目录 `deploy/build.sh` 的完整包内含该子包；分离安装脚本用 `--knowledge-hub-package` 或交互提问部署到 `INSTALL_ROOT/knowledge-hub`。
- 运行配置：部署脚本读写所在目录的 `.env`（源码为 `knowledge-hub/deploy/.env`，权限 600，不入库；旧位置 `deploy/.env` 会在首次部署时迁移过来）。`HUB_SERVICE_TOKEN`、`HUB_EVALUATION_SERVICE_TOKEN` 缺失时生成；整栈脚本和安装脚本优先沿用 onto-modeling 已有的 `ONTO_HUB_SERVICE_TOKEN`，并在部署 onto-modeling 前把它对齐为 Hub 实际使用的值。`CONSUMER_NETWORK` 由编排脚本现场探测注入；手动运行包内 `deploy.sh` 时在 `.env` 或环境中显式设置，单独部署的 onto-modeling 填其 compose 网络（如 `onto-web_default`）。`HUB_AUTH_URL`、`HUB_AUTH_ISSUER`、`HUB_CONSUMER_SERVICE_TOKEN` 也写在该 `.env`。
- 从 Hub 随 onto-modeling 部署的旧版本切换时，重新部署 onto-modeling 会以 `--remove-orphans` 清理旧 Hub 容器；旧卷（`onto-web_knowledge-hub-data` / `onto-modeling_knowledge-hub-data`）不自动迁移，确认无数据后可手动删除。

## 数据与恢复

```text
packages/<ontology_id>/
  metadata.json
  .git/
  releases/<release_id>/{receipt.json,package.zip}
  snapshots/<release_id>/{receipt.json,package.zip}
  pending/<internal_request_id>/
  resources-current/{receipt.json,resource/...}
publish-requests/<internal_request_id>.json
.lock
```

发布先保存预留请求，再上传校验。只有重新授权并持久化的 committing 意图能在恢复时完成归档；Git 成功后才公开 committed。仅 uploaded 的内容不会自动发布。ZIP 不变，下载校验原摘要；mapping 派生必须保留基础版本引用。当前支持 schema 0.1 / 0.2（operation-centric.v1），包根与 ontology_id（manifest.yaml.id） 一致。

本体授权用户可读、写、授权、撤权和删除；禁止撤销最后一人。删除保留 metadata 墓碑。共享资料按 generation 全量替换或清空，仅建模可写、全部服务可按用户权限读，不保留历史资料。建模只同步显式选择的资料，不后台自动上传。

## 备份

```bash
cd knowledge-hub/backend
python -m hub.backup backup /data/knowledge-hub /backup/hub-current
python -m hub.backup restore /backup/hub-current /restore/knowledge-hub \
  --current-metadata /current-hub/packages
```

备份持有全局写锁，包含原 ZIP、请求日志、授权集合、删除墓碑和最新资料。恢复目标必须为空；必须提供停写后的当前 Hub packages 元数据目录，以覆盖旧备份中的 ACL 和墓碑，避免恢复已撤销权限或已删除包。不要使用建模项目删除记录。恢复期间源目录必须保持不变；恢复前保管当前元数据副本。建模本地项目关联和用户请求缓存在 hub-tasks/packages/，不保存用户令牌。

支持单实例本地卷，不支持多副本共享网络文件系统。监控容量和归档 Git 增长；空间不足或归档失败不得报告成功。

## 验证

```bash
cd knowledge-hub/backend
.venv/bin/python -m pytest tests
```

测试仅用合成用户、合成 ZIP、临时凭据及隔离运行目录。覆盖服务/用户权限、mapping 差异、无项目导入、幂等、恢复和备份。协议见 [API.md](API.md)，设计见 [独立包决策](../docs/decisions/hub2-independent-packages.md)。
