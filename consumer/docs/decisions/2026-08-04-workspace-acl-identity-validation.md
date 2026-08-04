# Workspace ACL 身份引用与部署迁移规则统一

> 状态：已实施 · 2026-08-04

## Context

workspace 的 `owner` 和 `members` 保存用户名引用，但原运行时接口只检查字符串格式，不检查认证目录中是否存在该用户。管理员因此可以写入任意用户名；部署迁移 `migrate_auth_workspace.py` 又会严格审计这些引用，并以 `WORKSPACE_ACL_RECONCILIATION_REQUIRED` 阻止启动。运行时写入规则与升级规则不一致，导致正常使用接口制造的数据在后续升级时成为 `unknown_member` 或 `unknown_owner`。

用户删除同时会清理 workspace 成员。如果“校验用户存在”和“写入 workspace”之间恰好发生删除，仅增加一次存在性查询仍存在 TOCTOU 竞争，也会重新制造孤儿引用。

## Decision

1. 新增应用服务 `backend/api/workspace_acl_service.py`，集中处理 workspace ACL 与认证目录之间的跨系统校验；文件存储层 `workspace/workspaces.py` 保持纯持久化与授权职责，不直接依赖远程认证服务。
2. 新增 `POST /agentic/workspaces/{workspace_id}/members/validate` 供前端添加草稿成员时即时校验。`PUT /members` 保存完整列表时必须再次最终校验，不能信任先前校验结果。
3. 非空 owner/member 写入都必须出现在认证目录中。目录不可用时 fail closed：返回 `AUTH_DIRECTORY_UNAVAILABLE`，不写 ACL。空成员列表和 `owner=null` 不创建身份引用，允许用于清理和公开化。
4. ACL 校验与写入使用用户删除流程已有的身份生命周期锁，锁顺序统一为“按用户名排序的身份锁 → workspace 元数据锁”。待处理 deletion reconciliation 会返回 `USER_DELETION_RECONCILIATION_PENDING`，不能把相同用户名重新写回 ACL。
5. 批量成员保存只读取一次认证目录，并一次性返回缺失用户名；整批校验失败时不做部分写入。
6. 存量 ACL 的部署迁移继续严格阻断，并与运行时采用相同格式规则：owner/member 必须是无首尾空格的非空字符串，只有 owner 缺失或为 `null` 才表示公共空间。迁移只报告 findings 和中文修复建议，不自动创建用户、不移除成员、不改 owner，也不增加跳过校验或自动放行参数。
7. 发布前先执行 migration dry-run。`unknown_member` 由管理员选择创建同名账号或移除成员；`unknown_owner` 提示管理员创建同名账号，或通过 workspace API 转移给已验证账号。修复后必须重新 dry-run，确认 `workspace_acl.clean=true`。

## Alternatives considered

- 只在前端下拉框限制可选用户：拒绝。调用者可直接请求 API，且无法解决保存与删除的并发竞争。
- 只在添加按钮时校验、PUT 不复核：拒绝。即时校验到最终保存之间用户可能已被删除。
- 在 `workspace/workspaces.py` 中直接请求 auth-server：拒绝。会让底层文件存储依赖远程服务，并影响迁移、测试和内部调用。
- 迁移发现孤儿引用时自动删除、自动设为公共空间或忽略错误：拒绝。这些操作会改变访问权限语义，必须由管理员明确决定。
- 为兼容历史数据增加 `--allow-orphan-acl`：拒绝。会继续扩大运行时与部署规则差异，并保留同名账号继承权限的风险。

## Consequences

- 通过 workspace HTTP 接口不能再写入认证目录不存在的 owner/member，迁移脚本与运行时采用同一身份存在性规则。
- 用户删除与 ACL 写入在当前单进程多 gthread 部署中串行化；批量锁按用户名排序，避免不同成员顺序导致死锁。
- auth-server 短时不可用会阻止新增或替换非空 ACL，这是为避免无效权限引用采取的 fail-closed 行为；管理员仍可清空成员或把 owner 设为 `null`。
- 用户删除 reconciliation 状态损坏或无法读取时，ACL 写接口返回 `503 USER_DELETION_STATE_UNAVAILABLE`，不会把异常状态降级为可写。
- 存量孤儿 ACL 不会被静默改变。升级仍会失败，直到管理员根据业务含义完成修复并通过 dry-run。
- 生命周期锁是进程内锁，与当前单进程部署一致；若未来改为多 worker，需要引入跨进程/分布式协调后再调整该约束。
