# Knowledge Package Hub

Hub2 保存用户所属的不可变知识包，支持建模发布、无项目外部包接收、mapping 派生版本校验、查询和下载。建模侧已接入；独立评测在后续 eval1 实施，消费服务适配在后续阶段实施，本期不修改消费。

无 Hub1 存量数据，旧项目包 API 和存储实现已移除，不提供迁移、复制或双链路兼容。项目最新资料仍是独立业务，按项目权限同步、下载和清理，不进入知识包。

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
- `HUB_AUTH_ISSUER`：包所有权使用的稳定身份命名空间，未设置时等于 HUB_AUTH_URL。运行后不要随意改变，否则所有权键会变化。
- `HUB_SERVICE_TOKEN`：与建模 `ONTO_HUB_SERVICE_TOKEN` 对应，是建模访问包 API、项目资料及授权回调的统一服务身份。沿用部署脚本已有的自动生成逻辑，不按开发阶段另设凭据。
- 后续接入的评测/消费分别使用 `HUB_EVALUATION_SERVICE_TOKEN`、`HUB_CONSUMER_SERVICE_TOKEN`，两者与建模凭据必须不同；未接入服务可不配置。
- `ONTO_AUTHZ_URL`：项目资料的权限回调地址。包 API 直接使用统一用户鉴权；资料的开发免登录开关不适用于包 API。
- 包上传默认 256 MiB，解压总量 1 GiB、单文件 256 MiB、成员数 10000；pending 默认保留 7 天。已占版本不会回收，已提交包不按 pending TTL 清理。

建模源码 Compose 和发布 Compose 已传递上述 Hub 配置，缺少统一服务身份时失败关闭。接入外部账号服务时同时设置 Hub 的可达地址及稳定 issuer 命名空间，不仅设置建模的认证地址。生产凭据由受控运行环境注入，不写入仓库。

## 数据与恢复

```text
packages/<hub_package_id>/
  metadata.json
  .git/
  releases/<release_id>/{receipt.json,package.zip}
  snapshots/<release_id>/{receipt.json,package.zip}
  pending/<internal_request_id>/
publish-requests/<internal_request_id>.json
projects/<project_id>/resources-current/
cleanup/<project_id>.json
.lock
```

发布先保存预留请求，再上传校验。只有重新授权并持久化的 committing 意图能在恢复时完成归档；Git 成功后才公开 committed。仅 uploaded 的内容不会自动发布。ZIP 不变，下载校验原摘要；mapping 派生必须保留基础版本引用。当前支持 schema 0.1 / 0.2（operation-centric.v1），包根与 manifest ID 一致。

用户私有包不随项目或消费空间删除。项目清理只影响最新资料及建模本地关联。项目资料按 generation 全量替换并可靠重试，不保留历史资料版本。

## 备份

```bash
cd knowledge-hub/backend
python -m hub.backup backup /data/knowledge-hub /backup/hub-current
python -m hub.backup restore /backup/hub-current /restore/knowledge-hub \
  --deletion-records /restore/onto/hub-tasks/cleanup
```

备份持有全局写锁，包含包归档、请求日志、所有权、最新资料及清理记录。恢复目标必须为空，重放最新项目资料删除记录，校验请求索引、版本唯一性、基础引用和 ZIP 摘要。项目清理不得触及 `packages/`。建模本地包关联/冻结输入保存在 `hub-tasks/packages/`，导入任务在 `hub-tasks/package-imports/`，均随 `hub-tasks/` 备份，不保存 access/refresh token。

支持单实例本地卷，不支持多副本共享网络文件系统。监控容量和归档 Git 增长；空间不足或归档失败不得报告成功。

## 验证

```bash
cd knowledge-hub/backend
.venv/bin/python -m pytest tests
```

测试仅用合成用户、合成 ZIP、临时凭据及隔离运行目录。覆盖服务/用户权限、mapping 差异、无项目导入、幂等、恢复和备份。协议见 [API.md](API.md)，设计见 [独立包决策](../docs/decisions/hub2-independent-packages.md)。
