# 跨入口 Workspace 强制与用户删除对账

> 状态：已实施 · 2026-07-13 · 迁移分支 `codex/consumer-main-full-migration`

## Context

Phase 2 把本地最新的 session/run 存储边界与远端的 workspace owner/member 模型合并。仅在普通 HTTP workspace 解析器中校验 ACL 仍有四类缺口：run/session 入口可能先查询全局 registry，ACP `auth=none` 可访问私有空间，MCP token 在成员被移除后仍可继续使用，删除身份后 consumer 侧成员关系和用户目录可能残留。

用户删除还存在崩溃窗口：若先删除远端身份，再写本地清理记录，进程在两步之间退出时，同名账号可以重新创建并继承残留权限。

## Decision

1. Workflow、session、A2A、ACP、memory 和模型配置入口必须先解析请求 workspace 并执行当前 ACL，再读取 run store、session store 或全局 registry。session 的实时状态补充同时匹配 `user_id + workspace_id`。
2. ACP `auth=none` 只允许访问默认 workspace 或无 owner workspace。该规则取代 `2026-07-10-workspace-ownership-authz.md` 中“无用户上下文全部放行”的旧决定。
3. MCP 每次解析已签名 token 后重新读取 workspace metadata。owner、member、默认空间和无 owner 空间可用；成员被移除、workspace 不存在或 metadata 损坏时立即拒绝。MCP token 不携带可实时验证的全局角色，因此不为名为 `admin` 的 subject 提供隐式绕过。
4. 删除用户前先拒绝仍拥有 workspace 的账号，再原子写入 `user-deletion-reconciliation.json` intent，随后删除远端身份并独立执行成员清理、runtime/memory 归档和 MCP token 吊销。完成后删除 intent；部分失败保留可重试记录。
5. 管理员建号和开放注册代理在整个创建请求期间持有同名生命周期锁。任何未完成 intent 都返回 `409 USER_DELETION_RECONCILIATION_PENDING`。retry 在同一锁内重新确认身份不存在并完成清理。
6. ACL metadata、用户删除 intent 和审计读取均 fail-closed。审计脚本把 reconciliation 文件中的每条记录都视为未完成，不按中间状态过滤。

## Alternatives considered

- 只在 token 签发时校验 workspace：拒绝。成员移除和 workspace 删除无法立即生效。
- run/session 查询后再校验 ACL：拒绝。会泄露资源存在性，并允许全局 registry 的同名 ID 串入响应。
- 删除完成后才写 reconciliation：拒绝。无法覆盖远端删除成功后的进程崩溃窗口。
- 清理失败后允许同名账号先创建：拒绝。残留 members 和用户目录会被新身份继承。

## Consequences

- 被移除成员的旧 run、session 和 MCP token 在下一次请求立即失效。
- ACP 无认证通道不能再访问私有 workspace；需要私有访问时必须启用 JWT。
- reconciliation 文件损坏时创建账号返回 503，避免在状态不明时复用用户名。
- 生命周期锁和 workspace metadata 锁按 `identity -> workspace -> reconciliation` 顺序获取。当前单进程 gthread 部署成立；未来启用多 worker 或多副本前必须改为跨进程锁或数据库事务。
- auth-server 在 Compose 中不发布宿主端口，开放注册必须经 consumer BFF。最终入口切换前，旧 `agentic` 路由仍是迁移期代码，发布门禁必须确认它不可达。
