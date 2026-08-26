# 后端前向迁移框架

本目录承载可在兼容窗口结束后整体删除的 forward-only 迁移。应用启动和部署脚本只依赖 `migrations.runner`，不得直接导入、判断或传递某个具体 `migration_id`。

## 注册契约

`runner.py` 中每个 `RegisteredMigration` 必须声明：

- 稳定的 `migration_id` 与独立模块；
- `phase`：
  - `startup`：小型文件迁移，可在本地启动和普通 ASGI 启动时运行；
  - `deployment`：需要停止旧版本写入、全量扫描或可能耗时的迁移；
  - `external-admin`：外部存储管理迁移，不会由 Uvicorn worker 或普通部署迁移自动执行；
- `failure_policy`：
  - `blocking`：失败后新运行时无法安全读取数据，runner 必须失败；
  - `non-blocking`：核心服务有明确安全降级，runner 记录告警后继续，源数据必须保留以便下次重试。

启动上下文只选择 `startup`；普通部署上下文选择 `startup` 与 `deployment`；`external-admin` 必须显式选择通用的 `external-admin` 上下文。`deploy.sh` 和 `migrate.sh` 不传递迁移专用参数。

## 独立模块契约

具体迁移模块至少提供：

```python
MIGRATION_ID = "stable-id"

def execute_migration(
    runtime_root,
    *,
    dry_run,
    writes_stopped,
    from_version="",
    to_version="",
):
    ...
```

每个模块必须在 apply 内再次完成全量预检，并提供幂等完成标记。报告至少包含 `migration_id`、执行阶段、scope、计数、备份路径、错误码和失败对象标识；不得包含 secret、Token、密码哈希、会话正文或业务正文。

## 数据提交顺序

1. dry-run 只读取，不创建目录、密钥、标记或备份。
2. apply 先验证全部源、目标、密钥和冲突。
3. 破坏性改写前创建权限为 `0600` 的人工恢复副本并验证内容。
4. 使用临时文件、文件 `fsync`、`os.replace` 和目录 `fsync` 提交目标。
5. 先回读验证规范目标，再删除或改名旧源。
6. 完成标记只是提交记录；重复执行仍要验证规范目标。

同一文件上的有序迁移不会自动组成事务。`system-config-model-profiles-v1` 先提交模型/profile 结构，`system-config-secret-encryption-v1` 再加密规范位置上的明文 secret；第二步失败时保留第一步结果、阻止启动，并在密钥或配置修复后安全重试。两步 dry-run 都不写盘；第二步通过共享的纯内存 planner 预检第一步投影后的 secret 位置与加密 context，runner 不包含迁移专用分支。

迁移和部署均不提供自动 rollback。发布级恢复使用停写后由 `backup.sh` 创建的全量备份；迁移模块生成的副本只供人工排障与恢复，不会被运行时读取。

人工恢复副本可能包含明文 secret、Token、凭据或业务敏感数据，必须保持 `0600` 并只允许指定运维人员访问。副本保留到升级验收完成且对应兼容窗口关闭，之后由指定运维人员按报告中的 `backup_paths` 清点并安全销毁；runner 不自动恢复或销毁副本，也不得把副本内容复制到普通日志、工单或 PR。

## 当前登记项

| `migration_id` | 迁移目的 | 阶段 | 失败策略 | 失败边界 |
| --- | --- | --- | --- | --- |
| `ontology-data-sources-direct-v2` | 删除共享连接引用，将存量数据源展开为 ontology 私有直接配置。 | `startup` | `blocking` | 新运行时不能安全读取旧引用；任何 scope 失败都必须保留源数据并阻止启动。 |
| `observability-workspace-config-v1` | 删除全局观测配置，将其复制到规范 workspace，并清理旧性能日志。 | `startup` | `non-blocking` | workspace 目标缺失时观测安全降级为关闭，配置失败时保留全局源以便重试；旧日志清理失败仅告警。 |
| `system-config-model-profiles-v1` | 将旧 `task_llm` 与 `default_profile_id` 收敛为有目标校验的全局 profile ref。 | `startup` | `blocking` | 严格读取拒绝旧字段、重复 profile ID、悬空 ref 与新旧配置冲突；失败时不覆盖 `system_config.json`。 |
| `system-config-secret-encryption-v1` | 加密 system config 全部规范 secret 字段，并验证已有密文的密钥与 context。 | `startup` | `blocking` | security-critical；缺少或无效密钥、密文不可解密、加密/备份/原子提交失败均阻止启动，原文件在提交前失败时保持不变。 |

## 迁移退场

兼容窗口结束时，只删除：

- `REGISTERED_MIGRATIONS` 中对应登记项；
- 独立迁移模块；
- 对应迁移测试与临时兼容文档。

`app.py`、`deploy.sh`、`migrate.sh` 和本通用 runner 不应因某个迁移退场而修改。
