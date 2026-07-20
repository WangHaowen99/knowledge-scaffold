# Consumer 前端阶段 4：配置、管理与权限验收记录

## 1. 范围

- 分支：`codex/frontend-settings-admin`
- 基线：阶段 3 `codex/frontend-knowledge-evolution@4b0d544`
- 日期：2026-07-15
- 目标：在三级配置中心内补齐系统配置运维、全局与 Workspace 模型管理、Workspace / 用户管理、MCP 自服务和密码安全，并同步删除对应历史页面。
- 上一阶段：[阶段 3：知识包、图谱与 Evolution](./frontend-stage3-knowledge-evolution.md)
- 总体方案：[Consumer 前端收敛与能力加强计划](./frontend-convergence-and-enhancement-plan.md#阶段-4加强配置管理和权限)

## 2. 系统配置运维

全局“系统配置”统一承载后端允许的八个非模型池 section：`llm`、`storage`、`auth`、`agent`、`memory`、`web_search`、`direct_execution`、`skill_evolution`。模型池继续由独立模型页面管理 `llm_profiles`；后端已移除的全局 `observability` 不再出现在导航或请求中。

- 模板下载使用 `/api/system-config/template`，保留服务端文件名。
- JSON 导入先做格式、section 和文件大小校验，再展示覆盖范围与脱敏预览；确认前不发送写请求。
- 导入会剥离空密钥占位，空模型模板不会覆盖现有模型池，未知 section 会在前端阻止。
- 每个模块保存和连接测试都直接使用当前未保存草稿；测试结果、失败原因和 `tested_at` 独立于保存状态展示。
- 只读账号可以查看公开配置，不能触发测试、保存或导入。
- 密钥只根据 `has_*` 标志展示“已配置”，不回显明文；留空保留现有值，仅受支持的显式清除标志会删除密钥。

## 3. 模型池与 Workspace 模型

全局模型和 Workspace 私有模型统一进入正式 `features/settings/models`：

- 新增和编辑弹窗可以直接测试未保存草稿，空密钥不进入 payload。
- 保存和删除均 PATCH 完整模型数组，不调用不存在的 DELETE 接口。
- 全局模型删除前检查：至少保留一个模型、全局 main/task 默认引用、所有可访问 Workspace 的 main/task 运行时引用。
- 任一 Workspace 列表或运行时引用加载失败、引用格式无法识别时，删除 fail-closed。
- Workspace 私有模型删除前重新读取当前空间 runtime；main/task 仍引用时阻止删除。
- 点击删除和最终确认前各重新核对一次引用，降低并发修改窗口。
- Workspace 切换会中止加载请求，并通过 scope、sequence 和 loaded-scope 清理旧草稿、测试结果、弹窗和密钥输入；迟到响应不会渲染到新空间。

## 4. Workspace 与用户管理

Workspace 管理改为完整列表一次加载、客户端搜索和分页，保持后端当前无服务端分页的真实契约。页面提供刷新、新建、编辑、设为默认、删除结果反馈和当前空间删除后的安全回退。全局集合操作只对平台管理员开放；空间负责人仅能编辑后端允许的空间信息。默认空间保护、403、404 和 409 冲突分别展示。

用户管理保留创建、重置密码和删除：

- 页面只读取用户名、角色和首次改密标志，不保留密码明文。
- 新建和重置使用与后端一致的密码规则，服务端 violations 会落到逐项反馈。
- 当前账号不能通过管理员列表重置或删除，bootstrap `admin` 不能删除。
- 删除返回 202 时明确显示“账号已停用、资源仍在对账”；409 的 Workspace owner transfer 与 reconciliation pending 分开处理。
- 用户管理入口同时检查 capability 和管理员角色，服务端仍是最终授权点。

## 5. MCP 与密码安全

MCP 管理迁入 `features/settings/mcp`，按“当前登录用户 + 当前 Workspace”提供自服务，不再使用错误的管理员角色判断。

- 状态和 Token 列表请求使用 AbortController、scope key 和 sequence；Workspace 切换会清除一次性 Token 明文、复制反馈和所有弹窗。
- Token 列表只保留掩码元数据，不接受服务端意外返回的明文或 hash。
- 新 Token 明文仅在当前 Workspace 的当前页面会话展示一次，复制成功或失败均有可见反馈。
- 未登录、缺少 Workspace 或 capability 关闭时 fail-closed。

普通改密和首次强制改密统一复用 `features/auth/passwordPolicy`、规则列表和错误反馈。首次改密页面根节点使用正式设计 token，浅色和深色外观一致；修改成功后沿用认证 hook 的退出并重新登录语义。

## 6. 外观、作用域与状态

个人偏好增加“跟随系统 / 浅色 / 深色”显式入口，并继续保留主题色选择。选择结果即时持久化并更新 document 模式。

配置中心按 Workspace 对 Workspace 级面板以及个人长期记忆面板重挂载；模型和 MCP 内部再以 AbortController、scope 和 sequence 拒绝旧响应。所有新增面板覆盖 loading、empty、read-only、error、success 和 disabled 状态，表格在窄屏使用局部横向滚动，弹窗沿用统一 focus / Escape / aria 语义。状态反馈统一使用浅色/深色语义前景与表面 token；强制改密在 840px 以下切换单列，覆盖 768px 验收宽度。FieldRow 会为直接表单控件建立 aria 关联，模型核心控件另有显式可访问名称。

## 7. 同阶段删除范围

替代能力接入当前生产入口后，同一阶段删除：

- `src/components/ConfigurationCenter.jsx`
- `src/components/SystemSettings.jsx`
- `src/components/WorkspaceManagement.jsx`
- `src/components/WorkspaceRuntimeConfig.jsx`
- `src/components/UserManagement.jsx`
- `src/components/ChangePasswordModal.jsx`
- `src/components/DataConnectionManagement.jsx`
- `src/components/McpManagement.jsx`
- `src/components/ForceChangePasswordPage.jsx`
- `src/utils/accountAccess.js` 及其测试
- `src/utils/passwordPolicy.js`

当前入口可达性基线由 8 个批准的不可达模块降为 0。生产 JS/JSX 共 89 个，全部由 `src/main.jsx` 可达；仅聚合已动态拆分模块、但不再被生产代码引用的四个 settings barrel 同步删除。

## 8. 自动验收

### 8.1 前端

```bash
cd consumer/frontend
npm run lint
npm test
npm run build
node scripts/check-entry-reachability.mjs
```

- ESLint：通过，0 error / 0 warning。
- Node tests：201 项通过。
- Vite build：通过。
- 入口检查：89 / 89 个生产模块可达，批准和未批准孤儿模块均为 0。
- `git diff --check`：通过。
- 新增覆盖密码规则、用户 202/409 分类、Workspace 搜索分页与删除回退、MCP scope/一次性明文、模型 payload/引用保护、系统配置导入脱敏与模块 reducer。

### 8.2 后端

阶段 4 受影响后端回归 137 项通过，用例覆盖系统配置、平台配置、全局/Workspace 模型、Workspace runtime/observability/authz、用户安全与删除对账，以及 MCP route/scope/token 契约。

## 9. Bundle 对比

| 资源 | 阶段 3 | 阶段 4 | 变化 |
|---|---:|---:|---:|
| 入口 JS | 524.56 kB | 525.53 kB | +0.18% |
| 入口 JS gzip | 164.46 kB | 165.28 kB | +0.50% |
| 全局 CSS | 137.95 kB | 140.22 kB | +1.65% |
| 全局 CSS gzip | 25.47 kB | 25.97 kB | +1.96% |
| Config 基础 lazy JS | 61.84 kB | 29.60 kB / gzip 9.16 kB | -52.13% |
| Models lazy JS / CSS | — | 31.66 / 2.26 kB | 新增按 section 加载 |
| System lazy JS / CSS | — | 22.65 / 5.64 kB | 新增按 section 加载 |
| MCP lazy JS / CSS | — | 14.30 / 3.31 kB | 新增按 section 加载 |
| Workspace Admin lazy JS | — | 10.73 kB | 新增按 section 加载 |
| User Admin lazy JS | — | 10.07 kB | 新增按 section 加载 |
| Admin 共享 lazy CSS | — | 3.09 kB | 新增按 section 加载 |
| Change Password lazy JS | — | 1.73 kB | 新增按需加载 |
| Knowledge lazy JS | 72.09 kB / gzip 23.52 kB | 72.09 kB / gzip 23.52 kB | 不变 |
| Observability lazy JS | 40.97 kB / gzip 12.90 kB | 40.97 kB / gzip 12.90 kB | 不变 |

入口资源增长低于 2%。配置中心改为 section 级直接动态导入：基础 chunk 较阶段 3 减少 52.13%，模型、系统、MCP、Workspace / 用户管理和改密只在进入对应 section 或打开弹窗时加载。Knowledge 与 Observability 的按需资源没有回归。

## 10. UI 验收边界

当前桌面浏览器安全策略拒绝继续访问本地 `http://127.0.0.1:6601`，本阶段没有绕过该限制，也没有声称完成真实浏览器截图验收。已自行完成的 UI 相关检查包括：生产构建、语义 token / 深色变量静态复核、三组响应式媒体规则、表格局部滚动、统一 Modal 语义、按钮 disabled/aria 状态，以及相关纯逻辑和请求契约测试。

本阶段没有执行系统配置导入、模型保存/删除、Workspace/用户/Token 写入或真实改密等破坏性操作。写路径由请求 payload 测试和后端契约回归覆盖。1440 / 1024 / 768、真实深色视觉、焦点恢复和控制台检查需要在允许访问本地页面的浏览器会话中补做。

## 11. 阶段结论

阶段 4 的代码、自动门禁、后端契约和可静态验收项已完成：系统配置具备模板、预览导入和分模块测试，模型删除具备引用一致性保护，Workspace / 用户管理补齐效率与对账反馈，MCP 与密码安全迁入正式 feature，作用域和密钥边界明确，对应历史实现已删除，入口批准孤儿模块归零。

下一阶段按[总计划的阶段 5](./frontend-convergence-and-enhancement-plan.md#阶段-5最终物理收敛)继续正式目录、入口、样式和 import 边界收口。
