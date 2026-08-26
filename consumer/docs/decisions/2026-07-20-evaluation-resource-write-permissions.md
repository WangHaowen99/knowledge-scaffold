# 工作空间 owner 的评测资源写权限与自助创建

> 状态：已实施 · 2026-07-20

## Context

建模工作台使用当前登录用户的 token 调用 consumer，需要创建项目对应的评测工作空间，并在其中导入知识包、激活版本、维护知识包内容和测试本体数据源。consumer 原先把工作空间创建和高风险知识包操作限制为全局 admin，导致普通建模用户无法独立完成评测工作空间生命周期。

consumer 前端同时把知识包导入、版本激活、删除、状态修改、文件编辑和数据源管理合并在 `ws.kb.write` 权限下。直接向所有普通用户（包括 workspace member）开放该权限会连带暴露未授权的高风险管理操作。

## Decision

1. 普通已认证用户可创建 workspace。非 admin 创建时，服务端忽略请求中的 owner 和 `set_as_default`，强制将 owner 设为当前用户，且不自动把首个自建空间设为全局默认；全局 admin 仍可指定 owner 和设置默认 workspace。
2. workspace owner 可删除自己拥有的 workspace，普通 member 不可删除。owner 删除自己拥有的默认 workspace 时，系统显式清空全局默认，不自动提升其他 workspace；全局 admin 删除默认 workspace 前仍需先切换默认。
3. 所有 workspace 资源写操作仅允许全局 admin 或当前 workspace owner，并由 `resolve_workspace_owner_or_admin_id` 在服务端强制校验；普通 member 只能读取、使用资源和执行评测：
   - `POST /agentic/ontologies/import-zip`
   - `PUT /agentic/ontologies/{ontology_id}/active-version`
   - `PUT /agentic/ontologies/{ontology_id}/data-source`
   - `DELETE /agentic/ontologies/{ontology_id}/data-source`
   - `PUT /agentic/ontologies/{ontology_id}/status`
   - `DELETE /agentic/ontologies/{ontology_id}`
   - `DELETE /agentic/ontologies/{ontology_id}/versions/{version}`
   - `PUT /agentic/ontologies/{ontology_id}/file`
   - `POST /agentic/ontologies/{ontology_id}/upload`
   - `POST /agentic/ontologies/{ontology_id}/data-source/test`
4. consumer 前端权限点：
   - `workspace.create`：所有登录用户可用。
   - `workspace.manage`：仍仅用于全局 admin 的跨 owner 删除、设默认等集合管理操作；owner 删除由 workspace 归属关系单独判断。
   - `ws.kb.import`：全局 admin 或 workspace owner。
   - `ws.kb.activate`：全局 admin 或 workspace owner。
   - `ws.data_source.write`：全局 admin 或 workspace owner。
   - `ws.data_source.test`：全局 admin 或 workspace owner。
   - `ws.kb.write`：全局 admin 或 workspace owner，用于状态修改、删除、文件编辑和上传。
5. 评测统一通过 `POST /agentic/runs` 创建 Run，并从服务端返回的 events link 读取 SSE；现有认证与 workspace ACL 保持不变，member 仍可执行评测运行。

## Alternatives considered

- 把 owner 级管理操作开放给所有 workspace member：拒绝。成员关系只代表可使用空间，不应自动获得删除知识包、覆盖文件和外部连接测试权限。
- 仅把删除、文件编辑等操作限制为 owner，但允许 member 覆盖导入、激活和修改数据源：拒绝。这些操作同样会改变 workspace 的运行资源和评测结果。
- 工作空间创建继续保持 admin-only：拒绝。modeling 使用当前用户 token 时会再次卡在影子工作空间创建阶段。
- 由 modeling 使用全局 admin 服务账号：拒绝。会扩大凭证暴露面，并掩盖实际用户与 workspace 的审计归属。
- 仅在 modeling 内隐藏错误：拒绝。不能解决 consumer 服务端的真实授权失败。

## Consequences

- 普通用户可以创建归自己所有的评测 workspace，并在其中完成知识包和数据源的完整管理操作。
- 普通用户不能通过创建请求把 owner 指向其他用户，也不能替换现有全局默认 workspace。
- 普通用户可以删除自己拥有的 workspace；删除当前空间后，consumer UI 会切换到其余可访问空间或空状态。
- 自助创建仍受现有全局 `workspace_max_count` 限制；当前没有新增按用户配额，部署方需要按容量设置合理上限。
- workspace ACL 仍是服务端边界；对无权访问的私有 workspace，请求继续返回 `WORKSPACE_ACCESS_DENIED`。
- 普通 member 在可访问 workspace 中只能读取和使用资源、执行评测，所有知识包和数据源写操作均由全局 admin 或 workspace owner 完成。
- 默认 workspace 和无 owner workspace 仍按既有策略允许所有登录用户访问，但其中没有普通用户 owner，因此 owner 级高风险操作仍只允许全局 admin。
- consumer UI 只展示当前权限允许的操作，避免普通用户看到仍会返回 403 的管理按钮。
