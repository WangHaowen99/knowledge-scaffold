# Workspace 归属模型与服务端访问强制

> 状态：已实施 · 2026-07-10 · 分支 fix/workspace-authz
>
> 2026-07-13 更新：Decision 3 的 ACP `auth=none` 全放行规则已被
> `2026-07-13-cross-entry-workspace-and-user-deletion.md` 取代。匿名 ACP
> 现在只允许默认 workspace 或无 owner workspace。
>
> 2026-07-20 更新：Decision 5 中关于 workspace 创建、删除以及知识包和
> 数据源写操作维持 admin-only 的规则，已被
> `2026-07-20-evaluation-resource-write-permissions.md` 取代。当前规则为：
> 登录用户可创建并删除自己拥有的 workspace；workspace 资源仅全局 admin
> 或 workspace owner 可修改，member 只能读取、使用和评测。
>
> 2026-08-04 更新：Decision 5 中“members 只做格式校验、不校验用户存在性”
> 已被 `2026-08-04-workspace-acl-identity-validation.md` 取代。当前 owner/member
> 非空引用必须指向认证目录中的现存用户，并与用户删除流程使用同一身份生命周期锁。

## Context

workspace 此前只是文件系统目录（`workspaces/<id>/workspace.json`），元数据仅有 id/name/description/时间戳，无 owner、成员、ACL 概念。服务端对 `X-Workspace-Id` 只做格式与存在性校验（`agentic/workspaces.py` 的 `resolve_workspace_id`），从不校验"当前用户是否有权访问该 workspace"；`GET /agentic/workspaces` 对所有登录用户返回全量列表。任何登录用户把请求头换成他人的 workspace id 即可读写其中的知识包、数据连接、runtime 配置——水平越权。前端 `permissions.js` 的 `workspace_roles`（wsRoles）是纯 UI 预留，后端无对应实现。

## Decision

1. **归属模型**：workspace.json 新增 `owner`（str|null）与 `members`（str[]）。新建 workspace 的 owner 默认为创建者；**无 owner 字段的存量 workspace 视为公共**（所有登录用户可访问），保证升级不锁死现有部署，由管理员事后按需设置归属收紧。
2. **强制点**：`resolve_workspace_id`（携带 workspace 上下文路由的主收口）。访问规则：全局 admin → 通；无 owner → 通；默认 workspace → 通（公共落地空间语义）；user==owner 或 user∈members → 通；否则以 werkzeug HTTPException 形式返回 403 `{"error":"Forbidden","code":"WORKSPACE_ACCESS_DENIED"}`（穿透各调用方的 `except (KeyError, ValueError)` 包裹，无需全局 errorhandler）。**不经 `resolve_workspace_id`（从 URL 路径取 id）或复用持久化 id 的入口须各自补校验**：`GET /workspaces/<id>` 单查、`GET /workspaces/<id>/runtime-config`、ACP 续跑端点（execute/stream/await/interact）取持久化 `workspace_id` 后调用公开的 `enforce_workspace_access`——否则成为绕过面（越权读配置、成员被移除后旧 run 仍可续跑）。
3. **无用户上下文放行**：`g` 中没有登录用户时（如 ACP `auth=none` 通道、非请求上下文）保持原行为。本决策只修已登录用户之间的水平越权；机器通道的认证由其自身配置负责。
4. **列表过滤而非 403**：`GET /agentic/workspaces` 保持对所有登录用户 200，但按可见性过滤（admin 全量），每项加法追加 `owner`/`members`/`role`（当前用户与该空间的关系：owner→`admin`、member→`member`）。前端失效空间自愈逻辑依赖该端点保持 200。
5. **管理权限（历史基线）**：改名/描述放开给 owner（原 admin-only）；owner 变更仅 admin（含 `PATCH {"owner": null}` 清空为公共空间——body 缺 owner 键=不改，显式 null=清空）；新增 `PUT /workspaces/<id>/members`（admin 或 owner）。本决策最初只校验 members 格式、不校验用户存在性；该部分已于 2026-08-04 被新的 ACL 身份引用决策取代。创建/删除和资源写权限的后续变化见文首更新说明。
6. **前端接通 wsRoles 预留**：AuthContext 从列表条目的 `role` 派生 `user.workspace_roles`，`can()` 的 wsRoles 分支即刻生效；空间视角新增"成员"管理区。
7. **顺手加固**：workspace.json / runtime_config.json / workspace-settings.json 写入改为 tmp + `os.replace` 原子写，并以模块级 `RLock` 保护 read-modify-write（create/update/delete/members/runtime-config/legacy 迁移）——单进程 gunicorn 64 gthread 下有真实丢更新与并发迁移 500 风险。用 `RLock` 而非 `Lock`：`create_workspace` 持锁期间会经 `list_workspaces` 触发迁移路径，需同线程重入。**仅单进程有效**：多 worker 部署下线程锁不跨进程，此加固对进程内并发有效、对跨进程写竞争无效（当前部署为单进程多线程，见 Dockerfile）。

## Alternatives considered

- **member 关系存到用户对象（user.workspace_roles 由后端 users.json 承载）**：被否。`to_public_dict` 三字段形状被契约测试逐字锁定（ADR D4），且 remote auth 模式下用户存储在 auth-server，consumer 不可随意加字段；关系放 workspace 侧无此约束。
- **全局 before_request 校验 X-Workspace-Id**：被否。前端对包括 `/auth/me` 在内的所有请求都附带该头，全局校验会把持有失效 workspace id 的用户在 `/auth/me` 上 403，触发前端整体登出，自愈永远来不及执行。
- **存量 workspace 默认私有（owner 缺失=拒绝）**：被否。升级即锁死所有现有部署的全部空间，不可接受。
- **workspace 删除/创建也放开给 owner**：暂缓。保持 admin-only，控制本次改动面。

## Consequences

- 已登录用户之间的 workspace 越权被服务端强制阻断；403 形状与既有契约一致（`{"error":"Forbidden"}` + 加法 code），前端不会误清 token。
- 存量部署升级后行为不变（全部空间仍公共），需管理员显式设置 owner 才收紧——这是兼容性与安全的权衡，部署文档应提示。
- 默认 workspace 恒公共：若管理员给默认空间设 owner，该 owner 设置对访问控制无效（仍全员可访问）；换默认空间前需自行评估。
- ACP `auth=none` 通道不受归属限制，其安全边界仍由 ACP 自身的 auth 配置决定。
- 会话中途被移出成员的用户，其后续请求收 403 就地报错，前端不主动切换空间（自愈仅在刷新/重登时执行）——可接受，后续可加全局 403 处理优化体验。
- 已知残留（均 low、fail-closed、无越权，未在本次扩大改动面修复）：① `workspace-settings.json` 的 `default_workspace_id` 读改写未纳入模块锁，并发设默认时 last-writer-wins（原子写保证不损坏，仅可能丢失并发更新，属既有形态）；② 前端换号的过渡态——`workspaceId` 仍取自 localStorage 直到 `loadWorkspaces` 校正，但 `wsRoles` 已在渲染期守卫清空且后端强制访问，故仅 UI 过渡态无提权。
