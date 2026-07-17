# Consumer 前端阶段 2：聊天、Trace 与观测验收记录

## 1. 范围

- 分支：`codex/frontend-chat-observability`
- 基线：阶段 1 `codex/frontend-chat-runtime@3022250`
- 日期：2026-07-15
- 目标：统一消息流与 Trace 的领域模型，补齐专业化消息表达、智能滚动、复制与反馈，并将会话性能和全局观测接入正式入口。
- 上一阶段：[阶段 1：聊天运行基础](./frontend-stage1-chat-runtime.md)

## 2. Canonical Conversation Model

`src/features/chat/lib/conversationModel.js` 成为聊天展示的唯一派生入口：

- 输入既支持当前消息缓存，也支持持久化 `agent.v2` envelope；后者先复用正式 `messageBuilder` 还原消息，再进入同一规范化流程。
- 用户消息、Agent 正文、reasoning、普通工具、业务数据查询、Web 搜索、子任务、运行状态和等待输入均转换为稳定领域块。
- 每个消息、块、进度项和子任务使用源事件标识生成稳定 key；缺少服务端标识时使用确定性 fallback，并处理重复标识。
- 子 Agent 事件与嵌套组持久化服务端 `seq` 作为 `sourceOrder`；同一毫秒内的 token、嵌套组和工具事件仍按 SSE 原始顺序展示。
- 根块严格保持 `streamSegments` 原始顺序，生命周期状态只在该回合末尾补充；工具、reasoning 和子任务不会跨回合重排。
- 开始、结束和耗时在规范化层统一计算；工具完整输出的 `output_ref`、`output_size` 和截断状态随领域块保留。
- `src/features/chat/lib/traceModel.js` 直接消费同一个 conversation model，通过 `sourceKey` 关联消息块和 Trace 节点；Trace 不再从原始消息独立推导第二套状态。
- 迭代指标作为回合附属指标保留，子 Agent 任务、嵌套任务、工具进度按原始父子关系和顺序进入 Trace。

`ChatWorkspace` 只负责会话编排和页面交互，正式展示拆分到：

- `ConversationView.jsx`：消息和专业化领域块。
- `TracePanel.jsx`：回合、迭代、工具和多 Agent 层级。
- `ToolOutput.jsx`：输出预览、截断提示与完整输出读取。
- `RunActions.jsx`：复制和运行反馈。

## 3. 消息与交互增强

- Agent 正文使用 Markdown；reasoning 摘要与详情、工具输入/输出、业务数据进度、Web 来源、多 Agent 子任务均有独立展示。
- 工具、回合和子任务展示运行状态与耗时；终态回合展示总耗时。
- `useSmartScroll` 依据距底部阈值维护跟随状态。用户主动上滚后停止强制滚动，并显示“返回最新消息”；回到底部后恢复跟随。
- 复制优先使用 Clipboard API，失败时使用临时 textarea fallback；成功、失败均有可见反馈。
- 运行反馈保留乐观交互，提交失败会回滚选择、展示错误并提供重试，不再静默吞掉失败。
- 等待输入、失败、取消和运行中状态使用同一 canonical model 的生命周期结果，现有输入/选择/审批提交入口保持可用。
- 成功、失败和取消会递归收口多 Agent 组、子任务及其未完成事件；Trace 终态下不会残留 running token 或工具节点。

## 4. 完整工具输出契约

### 4.1 Stream 事件

`tool.finished` 保留最多 4096 个字符的 `output_preview`，并增加：

| 字段 | 含义 |
|---|---|
| `output_truncated` | 预览是否被截断 |
| `output_ref` | 32 位十六进制不透明引用 |
| `output_size` | 完整输出的 UTF-8 字节数 |

普通、并行、子 Agent 和 direct execution 的工具结束事件均使用同一输出持久化入口。持久化失败不会阻断原有工具事件，事件仍返回预览和真实字节数。

### 4.2 读取 API

```http
GET /agentic/sessions/{session_id}/runs/{run_id}/tool-outputs/{output_ref}
```

成功响应包含 `output_ref`、`run_id`、`tool_call_id`、`content_type`、`output_size` 和 `output`；响应设置 `Cache-Control: no-store`。

安全边界：

- API 必须通过认证，并从当前用户与 Workspace 的 store 读取会话。
- 会话、run 和引用必须同时匹配；其他会话、其他 run、其他用户/Workspace 或非法引用均不能读取。
- `output_ref` 只接受 32 位十六进制值，run 目录使用 run id 的 SHA-256 摘要，不把外部标识直接作为磁盘路径。
- 前端仅在取得有效会话 id、run id 和不透明引用后构造编码 URL；加载失败、历史记录无引用和重试状态均有明确提示。

生命周期：

- 完整输出写入会话目录的 `tool-outputs/<run-key>/<output_ref>.json`，通过临时文件和原子替换提交。
- session compaction 只折叠事件日志，不删除完整输出。
- 支持按 run 清理完整输出；删除或裁剪 session 时随整个会话目录一起删除。
- 读取时复核记录中的 session、run、ref 和 UTF-8 大小，拒绝损坏或错配的 artifact。

## 5. 正式可观测性入口

新增 lazy-loaded `src/features/observability/**`，并从当前应用壳接入两个入口：

- 会话工具栏：当前会话性能分析和运行详情。
- 左侧管理导航：具有 `observability.global` capability 的用户可进入全局运行观测。

能力范围：

- 会话视图读取 `/agentic/sessions/{session_id}/performance`，展示汇总、趋势、运行列表、诊断和选中 run 详情。
- 全局视图读取 `/api/local-observability/overview` 与 `/api/local-observability/runs`，支持时间窗口、Workspace、状态、模型 provider 和 Agent 模式筛选及分页。
- run 详情读取 `/agentic/performance/runs/{run_id}`；会话视图固定 `session_id`，全局视图保留时间与 Workspace 作用域。
- 正式状态模型覆盖未登录、缺少会话、无权限、加载、空数据、错误、带旧数据刷新失败和可用状态。
- 历史摘要仍存在但 run 明细已被本地保留策略清理时，界面显示“运行明细已过期”，保留摘要与其他轮次选择，不暴露后端原始 404 文案。
- 页面显示本地观测、关闭观测或 Langfuse 写入模式，保留日志解析、未知 schema、缓存和扫描诊断。
- 观测 JS/CSS 独立 lazy chunk，不进入聊天首屏同步代码。

## 6. 同阶段删除范围

### 6.1 旧入口和组件

- `src/App.jsx`
- `src/components/AgenticChat.jsx`
- `src/components/ChatInput.jsx`
- `src/components/ChatMessage.jsx`
- `src/components/ChatSidebar.jsx`
- `src/components/TaskDAGSidebar.jsx`
- `src/components/RunFeedback.jsx`
- `src/components/SessionPerformanceAnalysis.jsx`
- `src/components/PerformanceRunDetail.jsx`
- `src/components/LocalObservabilityDashboard.jsx`
- `src/components/performance/PerformanceCharts.jsx`
- `src/components/LoginPage.jsx`
- `src/components/TopBar.jsx`

`src/App.jsx` 原计划在最终结构收敛时删除。本阶段删除聊天、Trace、性能、登录和导航旧组件后，它只会保留不可达且无法解析的旧引用，因此随替代实现一并提前删除；唯一生产入口仍为 `src/main.jsx`。

### 6.2 专属工具函数

- `src/utils/conversationTurns.js`
- `src/utils/nodeNames.js`
- `src/utils/observabilityStatus.js`
- `src/utils/performanceMetrics.js`
- `src/utils/reasoningDetailGroups.js`
- `src/utils/segmentGroups.js`
- `src/utils/toolCallTracker.js`
- `src/utils/webSearchDisplay.js`

相关旧测试同步删除；能力回归由 canonical model、Trace model 和 observability model 测试接管。

## 7. 自动验收

### 7.1 前端

```bash
cd consumer/frontend
npm run lint
npm test
npm run check:entry
npm run build
```

- ESLint：通过，0 error / 0 warning。
- Node tests：102 项通过。
- Vite build：通过。
- 新增回归覆盖 canonical 消息顺序与稳定 key、回合/工具/迭代耗时、Trace source linkage、嵌套子任务顺序、完整输出引用传递和安全 URL 构造、Web/业务数据专业化、智能滚动阈值、Clipboard fallback、观测权限/URL/筛选/分页/状态与 API 错误。

### 7.2 后端

重点回归：

```bash
cd consumer/backend
PYTHONPATH=. pytest \
  tests/test_chat_store.py \
  tests/test_stream_protocol_v2.py \
  tests/test_orchestrator_runtime.py \
  tests/test_session_routes.py \
  tests/test_perf_analysis_routes.py -q
```

- 受影响回归：65 项通过。
- 全量后端回归汇总：1114 项通过，2 项跳过。
- 沙箱环境中的 14 个端口绑定失败属于运行环境限制；对应网络用例在允许端口绑定的环境重跑，20 项通过。
- 新增回归覆盖完整输出跨 compaction 保存、run/session 删除生命周期、非法或跨作用域读取、API `no-store`、4096 字符预览、UTF-8 字节数及常规/并行/子 Agent/direct execution 写入。

## 8. 入口依赖图

| 指标 | 阶段 1 | 阶段 2 | 变化 |
|---|---:|---:|---:|
| 生产 JS/JSX 模块 | 79 | 77 | -2 |
| `main.jsx` 可达 | 45 | 64 | +19 |
| 批准的不可达模块 | 34 | 13 | -21 |
| 未批准孤儿模块 | 0 | 0 | 0 |

阶段 2 剩余 13 个白名单文件：

- 组件：`ChangePasswordModal`、`ConfigurationCenter`、`DataConnectionManagement`、`OntologyManagement`、`OntologySelector`、`OperationEvolution`、`SystemSettings`、`UserManagement`、`WorkspaceManagement`、`WorkspaceRuntimeConfig`。
- 工具：`accountAccess`、`ontologyCommand`、`ontologyImportDisclaimer`。

这些对象分别由阶段 3 和阶段 4 迁移，不再承载聊天、Trace 或观测能力。

## 9. Bundle 对比

| 资源 | 阶段 1 | 阶段 2 | 变化 |
|---|---:|---:|---:|
| 入口 JS | 483.38 kB | 516.38 kB | +6.83% |
| 入口 JS gzip | 152.45 kB | 161.77 kB | +6.11% |
| 全局 CSS | 283.28 kB | 164.83 kB | -41.81% |
| 全局 CSS gzip | 49.51 kB | 29.93 kB | -39.55% |
| Knowledge lazy chunk | 28.31 kB | 28.31 kB | 0 |
| Config lazy chunk | 61.84 kB | 61.84 kB | 0 |
| Observability lazy JS | — | 40.97 kB / gzip 12.90 kB | 新增 |
| Observability lazy CSS | — | 17.62 kB / gzip 3.62 kB | 新增 |

入口 JS 与 gzip 增量均低于 10% 阶段门槛。删除旧聊天、Trace 与观测专属样式后，全局 CSS 下降 41.81%。Vite 仍提示入口 chunk 超过 500 kB；新增观测能力已独立懒加载，该提示作为最终结构拆分阶段的持续优化项。

## 10. 阶段结论

代码与自动化门禁已满足阶段 2 的退出条件：主消息流和 Trace 使用同一领域模型，完整工具输出具备受控读取和完整生命周期，聊天交互与正式观测入口已替代历史实现，旧聊天/Trace/反馈/性能代码已删除。

## 11. UI 验收

使用当前分支独立 Vite 服务 `127.0.0.1:6601` 与本地后端完成：

- 登录和历史会话恢复成功；真实四轮会话按服务端事件顺序展示正文、业务数据工具、取消/成功终态、反馈和耗时。
- Trace 与正文来自同一模型，轮次、迭代、工具、run id 和终态对应一致。
- 会话性能入口可读取 4 个运行摘要；已清理的 run 明细按“运行明细已过期”降级，摘要保持可用。
- 管理员全局观测入口、时间窗口、筛选和“当前未开启观测”运行时状态可用；空窗口使用正式空状态。
- 1440、1024、768 三档宽度均无页面级横向溢出；窄屏导航和 Trace 使用独立遮罩抽屉，关闭 Trace 后对话区恢复全宽。
- 用户上滚后出现“返回最新消息”，点击后距底部 1 px、按钮消失并恢复跟随。
- 页面完整刷新后的浏览器控制台 error：0；Trace 分隔器可通过方向键、Home/End 调宽，ARIA 数值同步更新。

截图：

- [聊天 1440 × 1000](../../frontend/local-browser-artifacts/frontend-stage2/chat-1440.png)
- [聊天 1024 × 900](../../frontend/local-browser-artifacts/frontend-stage2/chat-1024.png)
- [聊天 768 × 1000](../../frontend/local-browser-artifacts/frontend-stage2/chat-main-768.png)
- [窄屏 Trace 抽屉](../../frontend/local-browser-artifacts/frontend-stage2/chat-768.png)
- [会话性能 1440 × 1000](../../frontend/local-browser-artifacts/frontend-stage2/session-observability-1440.png)
- [全局观测 1440 × 1000](../../frontend/local-browser-artifacts/frontend-stage2/global-observability-1440.png)
- [全局观测 1024 × 900](../../frontend/local-browser-artifacts/frontend-stage2/global-observability-1024.png)

阶段 2 退出条件全部满足。下一阶段按[总计划的阶段 3](./frontend-convergence-and-enhancement-plan.md#阶段-3加强知识包图谱和-evolution)继续知识包、图谱与 Evolution 收敛。
