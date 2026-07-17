# Consumer 前端阶段 0 基线与迁移台账

> 本文保留阶段 0 快照。阶段 1 的正式路径、状态所有权和验收结果见
> [frontend-stage1-chat-runtime.md](frontend-stage1-chat-runtime.md)。

## 1. 基线信息

- 记录日期：2026-07-15
- 分支：`frontend-unify`
- 基线提交：`4f327be096376eb73c99c254994a9fd7c0d9a640`
- 唯一生产入口：`consumer/frontend/src/main.jsx`
- 精确依赖图：`cd consumer/frontend && npm run check:entry -- --json`
- 门禁命令：`cd consumer/frontend && npm run lint && npm test && npm run build`

入口扫描只统计 `src` 下非测试的 JS/JSX 生产模块。当前共 76 个：

- 入口可达：42 个。
- 已批准但仍待迁移的不可达模块：34 个。
- 未登记不可达模块：0 个。
- 失效登记：0 个。
- 无法解析的相对 import：0 个。

`scripts/entry-reachability-baseline.json` 是不可达模块的机器基线。新增生产模块若未进入入口依赖树且未同步登记，`npm test` 会失败。

## 2. 入口依赖摘要

```text
main.jsx
└── redesign/RedesignApp.jsx
    ├── redesign/AuthContext.jsx
    │   └── hooks/useAuth.js
    └── redesign/AppShell.jsx
        ├── hooks/useStreamManager.js
        ├── hooks/useChatSessions.js
        ├── redesign/ChatWorkspace.jsx
        │   └── hooks/useAgenticStream.js
        ├── redesign/ChatHistorySidebar.jsx
        ├── redesign/KnowledgeWorkbench.jsx (lazy)
        ├── redesign/ConfigCenter.jsx (lazy)
        └── redesign/* 当前页面与 UI 基础模块
```

完整逐文件邻接表由入口检查脚本的 `--json` 输出提供，动态 `import()` 已纳入扫描。

## 3. 文件级迁移台账

状态定义沿用总计划：

- `covered`：当前入口已经覆盖，随其冻结依赖链一起删除。
- `migrate`：仍有能力或行为需要提取，迁移与删除必须在同一变更完成。
- `shared-active`：当前生产入口正在使用，后续迁入正式 `app/features/shared` 目录。
- `delete`：已证明无能力价值，可直接删除。

### 3.1 当前入口可达模块（42）

| 文件 | 状态 | 目标/责任 |
|---|---|---|
| `src/main.jsx` | `shared-active` | 最终只挂载 `app/App` |
| `src/components/ForceChangePasswordPage.jsx` | `shared-active` | `features/auth` |
| `src/components/McpManagement.jsx` | `shared-active` | `features/settings` |
| `src/hooks/useAgenticStream.js` | `shared-active` | `features/chat` |
| `src/hooks/useAuth.js` | `shared-active` | `features/auth` |
| `src/hooks/useChatSessions.js` | `shared-active` | `features/chat` |
| `src/hooks/useStreamManager.js` | `shared-active` | `features/chat` |
| `src/redesign/AppShell.jsx` | `shared-active` | `app/AppShell` |
| `src/redesign/AuthContext.jsx` | `shared-active` | `app/providers` 与 `features/auth` |
| `src/redesign/ChatHistorySidebar.jsx` | `shared-active` | `features/chat` |
| `src/redesign/ChatWorkspace.jsx` | `shared-active` | `features/chat` |
| `src/redesign/ConfigCenter.jsx` | `shared-active` | `features/settings` |
| `src/redesign/EvolutionWorkbench.jsx` | `shared-active` | `features/evolution` |
| `src/redesign/KbSidebarTree.jsx` | `shared-active` | `features/knowledge` |
| `src/redesign/KnowledgeDataSources.jsx` | `shared-active` | `features/knowledge` |
| `src/redesign/KnowledgeWorkbench.jsx` | `shared-active` | `features/knowledge` |
| `src/redesign/LoginPage.jsx` | `shared-active` | `features/auth` |
| `src/redesign/Markdown.jsx` | `shared-active` | `shared/ui` |
| `src/redesign/OnboardingWizard.jsx` | `shared-active` | `app` 或 `features/settings` |
| `src/redesign/OntologyGraphPanel.jsx` | `shared-active` | `features/knowledge` |
| `src/redesign/RedesignApp.jsx` | `shared-active` | `app/App` |
| `src/redesign/UiFeedback.jsx` | `shared-active` | `shared/ui` |
| `src/redesign/chatUtils.js` | `shared-active` | `features/chat/lib` |
| `src/redesign/permissions.js` | `shared-active` | `features/auth/lib` |
| `src/redesign/theme.js` | `shared-active` | `shared/styles` |
| `src/redesign/ui.jsx` | `shared-active` | `shared/ui` |
| `src/redesign/useAuthContext.js` | `shared-active` | `features/auth` |
| `src/redesign/useKbBrowser.js` | `shared-active` | `features/knowledge` |
| `src/redesign/version.js` | `shared-active` | `app` |
| `src/redesign/wizardState.js` | `shared-active` | `app` |
| `src/utils/auth.js` | `shared-active` | `shared/api` |
| `src/utils/logger.js` | `shared-active` | `shared/lib` |
| `src/utils/messageBuilder.js` | `shared-active` | `features/chat/lib` |
| `src/utils/onboardingLayout.js` | `shared-active` | `app/lib` |
| `src/utils/passwordPolicy.js` | `shared-active` | `features/auth/lib` |
| `src/utils/requestErrors.js` | `shared-active` | `shared/api` |
| `src/utils/sessionModel.js` | `shared-active` | `features/chat/model` |
| `src/utils/streamHelpers.js` | `shared-active` | `features/chat/protocol` |
| `src/utils/streamLifecycle.js` | `shared-active` | `features/chat/protocol` |
| `src/utils/streamResume.js` | `shared-active` | `features/chat/protocol` |
| `src/utils/workspacePreferences.js` | `shared-active` | `features/chat/model` |
| `src/utils/workspaceRuntime.js` | `shared-active` | `app/providers` |

### 3.2 当前入口不可达模块（34）

| 文件 | 状态 | 能力/删除边界 |
|---|---|---|
| `src/App.jsx` | `migrate` | 冻结旧入口；完成全部能力迁移后删除 |
| `src/components/AgenticChat.jsx` | `migrate` | 消息、滚动、会话性能入口 |
| `src/components/ChangePasswordModal.jsx` | `migrate` | 密码校验与账号操作反馈 |
| `src/components/ChatInput.jsx` | `migrate` | 输入、模式选择、快捷命令与交互 |
| `src/components/ChatMessage.jsx` | `migrate` | 专业消息、复制、工具与搜索展示 |
| `src/components/ChatSidebar.jsx` | `migrate` | 与当前历史会话侧栏逐项对照后删除 |
| `src/components/ConfigurationCenter.jsx` | `migrate` | 历史配置能力总入口 |
| `src/components/DataConnectionManagement.jsx` | `migrate` | 与两层数据源能力逐项对照 |
| `src/components/LocalObservabilityDashboard.jsx` | `migrate` | 本地观测与全局性能入口 |
| `src/components/LoginPage.jsx` | `covered` | 当前登录页已覆盖；随旧入口依赖链删除 |
| `src/components/OntologyManagement.jsx` | `migrate` | 导入、版本和安全确认能力 |
| `src/components/OntologySelector.jsx` | `migrate` | 选择与会话锁定交互 |
| `src/components/OperationEvolution.jsx` | `migrate` | Operation Evolution 完整生命周期 |
| `src/components/PerformanceRunDetail.jsx` | `migrate` | 运行详情与工具耗时 |
| `src/components/RunFeedback.jsx` | `migrate` | 回合反馈与失败反馈 |
| `src/components/SessionPerformanceAnalysis.jsx` | `migrate` | 会话性能分析 |
| `src/components/SystemSettings.jsx` | `migrate` | 模板、导入和分模块测试 |
| `src/components/TaskDAGSidebar.jsx` | `migrate` | Trace、完整输出和多 Agent 结构 |
| `src/components/TopBar.jsx` | `covered` | 当前 AppShell 已覆盖；随旧入口依赖链删除 |
| `src/components/UserManagement.jsx` | `migrate` | 当前账号保护、密码策略和权限反馈 |
| `src/components/WorkspaceManagement.jsx` | `migrate` | 搜索、分页、刷新和编辑反馈 |
| `src/components/WorkspaceRuntimeConfig.jsx` | `migrate` | Workspace 运行配置完整性 |
| `src/components/performance/PerformanceCharts.jsx` | `migrate` | 性能图表展示 |
| `src/utils/accountAccess.js` | `migrate` | 用户与当前账号保护纯逻辑 |
| `src/utils/conversationTurns.js` | `migrate` | 回合与 Trace 顺序映射 |
| `src/utils/nodeNames.js` | `migrate` | 节点与状态显示语义 |
| `src/utils/observabilityStatus.js` | `migrate` | 本地观测可用性判断 |
| `src/utils/ontologyCommand.js` | `migrate` | 聊天知识包快捷选择 |
| `src/utils/ontologyImportDisclaimer.js` | `migrate` | 导入安全确认边界 |
| `src/utils/performanceMetrics.js` | `migrate` | 会话与工具耗时计算 |
| `src/utils/reasoningDetailGroups.js` | `migrate` | reasoning 详情分组 |
| `src/utils/segmentGroups.js` | `migrate` | 消息片段顺序和分组 |
| `src/utils/toolCallTracker.js` | `migrate` | 工具调用关联和耗时展示 |
| `src/utils/webSearchDisplay.js` | `migrate` | 搜索来源与错误展示 |

## 4. 本批删除结论

阶段 0 未删除生产文件。34 个不可达模块中：

- 2 个为已覆盖页面，但仍属于冻结 `App.jsx` 的完整依赖链，随最终收敛一起删除。
- 32 个仍承载计划中明确要求迁移或对照的能力。
- `toolCallTracker.js` 虽无当前生产引用，但对应阶段 2 的工具调用关联、耗时和完整输出能力，不能按纯残留删除。

后续每次删除必须同时更新本台账和 `scripts/entry-reachability-baseline.json`。

## 5. 自动回归基线

统一 `npm test` 当前执行 46 项测试并追加入口可达性检查，覆盖：

- SSE envelope 去重、cursor 推进和恢复 URL。
- queued、running/streaming、reconnecting、awaiting、finished、error、cancelled 状态收敛。
- 等待用户输入的持久事件恢复。
- 会话置顶排序、乐观更新、服务端提交和定向回滚。
- 会话 metadata 中模型、知识包和 Agent 模式恢复。
- Workspace 切换时停止流、清空消息缓存和重置会话状态。
- 历史消息中的运行与锁定 metadata 保留。

## 6. 构建与页面基线

### 6.1 生产构建

| 资源 | 原始大小 | gzip |
|---|---:|---:|
| 入口 JS | 475.31 kB | 149.99 kB |
| 全局 CSS | 283.28 kB | 49.51 kB |
| KnowledgeWorkbench lazy chunk | 28.31 kB | 9.77 kB |
| ConfigCenter lazy chunk | 61.84 kB | 16.76 kB |

相对实施前入口 JS（473.68 kB / gzip 149.41 kB），阶段 0 增量为 1.63 kB / gzip 0.58 kB，均小于 1%。

### 6.2 页面加载

本机 Vite 开发服务、登录页、刷新导航一次：

- HTTP 状态：200。
- responseStart：42.4 ms。
- DOMContentLoaded：105.9 ms。
- loadEvent：107.8 ms。

该数据用于后续阶段相对比较，不作为生产环境绝对性能结论。

### 6.3 三档宽度截图

- [1440 × 1000](../../frontend/local-browser-artifacts/frontend-stage0/login-1440x1000.png)
- [1024 × 1000](../../frontend/local-browser-artifacts/frontend-stage0/login-1024x1000.png)
- [768 × 1000](../../frontend/local-browser-artifacts/frontend-stage0/login-768x1000.png)

三档宽度均显示完整登录卡片，无横向溢出、裁切或控件重叠。
