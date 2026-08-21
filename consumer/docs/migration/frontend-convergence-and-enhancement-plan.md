# Consumer 前端收敛与能力加强计划

## 1. 文档信息

- 跟踪 Issue：[manufacture-squad-scene/knowledge-scaffold#3](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/3)
- 主要关联 Issue：[对话状态与执行反馈 #4](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/4)、[会话性能与全局看板 #10](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/10)
- 目标仓库：`meta-wise`
- 目标分支基线：`develop@4f327be096376eb73c99c254994a9fd7c0d9a640`
- 对照仓库：`onto-agent`
- 对照分支基线：`origin/main@d1b6683ce3ea4246b6776f26d4680ab9b2d4888e`
- 文档状态：阶段 0—5 实施、自动门禁、受控本地浏览器功能/UI 与写路径、页面加载性能复测已完成；真实外部模型、业务数据库及 Evolution 成功链路待联调环境验收
- 更新日期：2026-07-16

## 2. 实施前背景

阶段 0 启动时，Consumer 前端存在两套页面实现：

- `consumer/frontend/src/App.jsx` 及 `src/components/**` 组成的历史前端。
- `consumer/frontend/src/redesign/**` 组成的当前运行前端。

当时 `consumer/frontend/src/main.jsx` 只挂载 `RedesignApp`，历史 `App.jsx` 不参与默认运行。运行前端仍复用部分历史目录中的 hooks、utils 和组件，例如：

- `src/hooks/useAuth.js`
- `src/hooks/useChatSessions.js`
- `src/hooks/useStreamManager.js`
- `src/hooks/useAgenticStream.js`
- `src/components/ForceChangePasswordPage.jsx`
- `src/components/McpManagement.jsx`

阶段 0 静态入口扫描发现，当时 `src` 下有 34 个非测试 JS/JSX 文件不在 `main.jsx` 的运行依赖链中。其中既有可直接删除的重复实现，也有本地观测、会话性能、专业化消息展示、Evolution 等仍需迁移的能力。

因此，本次工作不能按目录批量删除，也不能继续让两套前端长期并行维护。采用“按能力迁移、同 PR 删除对应旧实现”的方式逐步完成收敛。

本节和后续能力台账中的“当前”均指阶段 0 的实施前基线；最终落地状态以阶段 5 验收记录为准。

## 3. 决策

### 3.1 采用的执行方式

采用垂直能力切片：

1. 冻结历史前端，不再向历史入口增加修复或功能。
2. 建立旧文件与能力迁移清单。
3. 按聊天、知识、进化、配置、观测等业务域迁移能力。
4. 每个能力迁入当前运行前端后，在同一个 PR 中删除对应旧实现。
5. 所有能力完成后，将 `redesign` 从历史命名调整为正式应用结构。
6. 最终只保留一个生产入口、一套页面实现和一套共享基础设施。

### 3.2 硬性规则

- 历史 `App.jsx` 和只由它使用的页面不得再接受功能修改。
- 不允许先复制旧组件、后续再承诺清理；迁移和删除必须在同一个 PR 完成。
- 不允许通过注释、环境变量或手工修改入口保留第二套前端。
- 回退依赖 Git 提交、发布版本和部署回滚，不依赖仓库内第二套 UI。
- 从 Onto Agent 迁移行为、接口契约、状态语义和测试，不整体覆盖 Meta-wise 的 JSX、CSS 和信息架构。
- 当前运行链路仍引用的文件必须先迁移到正式目录并切换引用，随后才能删除旧位置。
- 一个业务能力只能有一个生产实现；测试夹具和 Story/原型不得进入生产入口依赖链。
- 每个 PR 必须保持可构建、可启动、可验收，不提交中间态残缺入口。

## 4. 目标

### 4.1 代码目标

- 前端只有一个入口和一棵生产依赖树。
- 删除 `src/App.jsx` 以及所有只服务历史入口的组件、样式和工具函数。
- 消除 `redesign` 的临时历史命名，形成按业务能力组织的正式目录。
- API 调用、状态管理和展示组件边界明确。
- 运行协议、会话状态和 Workspace 状态具有可回归的纯逻辑测试。
- 新增能力只能进入正式 feature，不再出现平行页面。

### 4.2 产品目标

- 保留 Meta-wise 已完成的新信息架构、视觉设计、权限模型和知识包工作台。
- 补齐 Onto Agent 主线已经验证、当前 Meta-wise 入口缺失或弱化的能力。
- 保证聊天、知识、数据、进化、配置和观测能力在统一前端中完整可用。
- 修复功能迁移后形成的交互、错误提示、加载状态、空状态和响应式问题。

### 4.3 非目标

- 本计划不重新设计后端 API。
- 本计划不整套恢复历史 UI。
- 本计划不通过长期兼容层维持两套组件。
- 本计划不引入新的大型状态管理或 UI 框架。
- 本计划不要求一次性重写全部前端。

## 5. 目标目录

最终落地结构：

```text
consumer/frontend/src/
├── main.jsx
├── app/
│   ├── App.jsx
│   ├── AppShell.jsx
│   ├── appMeta.js
│   ├── providers/
│   └── onboarding/
├── features/
│   ├── auth/
│   ├── chat/
│   ├── knowledge/
│   ├── evolution/
│   ├── settings/
│   └── observability/
└── shared/
    ├── api/
    ├── lib/
    ├── ui/
    └── styles/
```

目录职责：

- `app/`：唯一应用壳、Provider、导航和顶层页面编排。
- `features/`：按业务能力组织页面、组件、状态和领域逻辑。
- `shared/api/`：认证请求、错误规范化和 API 基础能力。
- `shared/lib/`：无 React 依赖的纯函数、协议解析和状态转换。
- `shared/ui/`：通用 UI 原语，不包含业务 API 调用。
- `shared/styles/`：设计变量、全局基础样式和少量跨 feature 样式。

禁止新增 `legacy/`、`old/`、`v1/` 或第二套应用入口目录。

## 6. 能力基线与处理结论

### 6.1 当前运行前端已经具备，迁移时必须保留

| 能力 | 当前实现 | 处理方式 |
|---|---|---|
| 排队运行与 SSE 游标续传 | `useStreamManager`、`useAgenticStream` | 迁入 `features/chat` 或 `shared/lib`，补回归测试 |
| 历史会话恢复运行 | `ChatWorkspace` | 保留运行语义，覆盖刷新、切会话和断线恢复 |
| 等待用户输入与继续执行 | `ChatWorkspace`、stream helpers | 保留 input/choice/approval 交互和历史恢复 |
| 会话置顶与固定分组 | `useChatSessions`、`ChatHistorySidebar` | 保留持久化、乐观更新和失败回滚 |
| 会话锁定模型、知识包和 Agent 模式 | `ChatWorkspace` | 明确服务端 metadata 与本地默认值边界 |
| 三层配置作用域 | `ConfigCenter` | 保留全局、Workspace、个人三级结构 |
| capability 权限控制 | `AuthContext`、`permissions.js` | 保留只读和可编辑边界 |
| 知识包文件与版本管理 | `KnowledgeWorkbench`、`useKbBrowser` | 保留查看、编辑、上传、下载、启停和版本操作 |
| 知识包私有数据源 | `KnowledgeDataSources` | 保留 provider 配置、密钥不回显和作用域隔离语义 |
| 用户长期记忆 CRUD | `ConfigCenter` 的个人记忆面板 | 保留列表、新增、编辑和删除 |
| Workspace 级模型、运行和观测配置 | `ConfigCenter` | 保留作用域和权限语义 |
| 主题、响应式应用壳和操作向导 | `redesign/**` | 迁入正式 `app/shared` 结构 |

### 6.2 从 Onto Agent 或历史实现补齐

| 优先级 | 能力 | 当前差距 | 目标 |
|---|---|---|---|
| P0 | 流式状态回归保护 | 关键能力存在，但缺少 hook 级完整回归 | 覆盖排队、重连、去重、结束、取消、等待输入 |
| P1 | 专业化消息展示 | 当前主要使用通用步骤行 | 补齐搜索来源、数据查询、子任务、工具耗时和错误详情 |
| P1 | 智能滚动与内容复制 | 当前每次消息变化直接滚到底部，复制反馈较弱 | 用户上滚后停止自动滚动，提供稳定复制状态和 fallback |
| P1 | 会话性能与本地观测 | 旧组件存在但当前入口不可达 | 接入会话性能、运行详情和本地观测入口 |
| P1 | Trace 完整输出与耗时 | 总耗时与反馈由 fork Issue #4 跟踪，完整输出仍由总 Issue #3 验收 | 展示回合、迭代、工具耗时和完整输出入口 |
| P1 | 本体图运行态 | 当前主要是静态 SVG 结构图 | 增加 Trace 命中、高亮、视图重置、全屏和图谱纯逻辑测试 |
| P1 | Skill Evolution 完整生命周期 | 当前实现缺少部分列表、Diff 和任务管理能力 | 增加按本体分组、轮询、删除、词级 Diff、漂移处理和审批模式 |
| P1 | 系统配置运维能力 | 缺少模板、导入和分模块测试入口 | 接入配置模板下载、导入确认、测试结果和错误反馈 |
| P1 | 模型配置运维能力 | 部分作用域缺少测试或完整删除 | 增加未保存配置测试、全局/空间模型删除和引用一致性保护 |
| P2 | Workspace 管理效率 | 当前列表适合少量数据 | 增加搜索、分页、刷新和编辑反馈 |
| P2 | 可访问性与键盘操作 | 新界面基础能力不一致 | 补齐焦点、dialog、菜单、Escape、aria 状态和键盘导航 |

### 6.3 Meta-wise 特有能力必须保留

- 新应用壳和统一左侧导航。
- 全局、Workspace、个人三级配置中心。
- capability 驱动的权限和只读展示。
- 知识包文件、版本、数据源、图谱和进化的一体化工作台。
- 知识包私有数据源配置及其密钥保护语义。
- 操作向导、主题和当前品牌视觉。

Onto Agent 的页面布局和样式不能覆盖这些能力。

## 7. 旧代码迁移清单

迁移开始前维护一张持续更新的台账。每个文件只能处于以下状态之一：

- `covered`：当前运行前端已经完整覆盖，可直接删除。
- `migrate`：包含仍需能力，迁移和删除必须同 PR 完成。
- `shared-active`：当前入口仍在复用，需要先移动并切换引用。
- `delete`：确认无能力价值，可直接删除。

初始分组如下：

| 领域 | 历史文件 | 初始结论 |
|---|---|---|
| 旧入口 | `src/App.jsx` | 最终删除；迁移期间冻结 |
| 聊天 | `AgenticChat.jsx`、`ChatInput.jsx`、`ChatMessage.jsx`、`ChatSidebar.jsx` | `migrate`，提取消息展示、滚动、复制和交互能力 |
| Trace | `TaskDAGSidebar.jsx`、相关 segment/reasoning utils | `migrate`，能力进入统一 Trace 后删除 |
| 性能 | `SessionPerformanceAnalysis.jsx`、`PerformanceRunDetail.jsx`、`performance/**` | `migrate`，接入正式观测 feature |
| 反馈 | `RunFeedback.jsx` | `migrate` 或由当前反馈实现覆盖后删除 |
| 知识 | `OntologyManagement.jsx`、`OntologySelector.jsx` | `migrate`，只提取当前工作台缺失能力 |
| 进化 | `OperationEvolution.jsx` | `migrate`，对照当前 EvolutionWorkbench 补齐后删除 |
| 配置 | `ConfigurationCenter.jsx`、`SystemSettings.jsx` | `migrate`，提取测试、模板、导入等能力 |
| Workspace | `WorkspaceManagement.jsx`、`WorkspaceRuntimeConfig.jsx` | `migrate`，提取搜索分页和缺失交互 |
| 用户 | `UserManagement.jsx` | 对照权限与用户管理能力后 `covered/delete` |
| 观测 | `LocalObservabilityDashboard.jsx` | `migrate` |
| 导航 | `TopBar.jsx`、历史 `LoginPage.jsx` | 当前应用壳覆盖后删除 |
| 当前复用组件 | `ForceChangePasswordPage.jsx`、`McpManagement.jsx` | `shared-active`，迁入正式 feature 后删除原位置 |
| 当前复用 hooks | `useAuth.js`、`useChatSessions.js`、`useStreamManager.js`、`useAgenticStream.js` | `shared-active`，迁入正式目录并补测试 |
| utils | `src/utils/**` | 按真实引用和能力逐个判断，禁止整目录保留 |

每个 PR 必须同步更新台账状态，不能留下没有负责人和迁移结论的文件。

## 8. 分阶段实施计划

### 阶段 0：固化基线和迁移边界

目标：在修改业务能力前建立可验证的基线。

工作项：

1. 记录当前 `main.jsx` 入口依赖图和不可达文件清单。
2. 为 `package.json` 增加统一的 `npm test` 命令，继续使用现有 `node:test` 基础。
3. 补充以下纯逻辑或 hook 行为测试：
   - SSE envelope 去重和 cursor 推进。
   - 排队、运行、重连、等待输入、完成、错误和取消状态转换。
   - 会话置顶排序、乐观更新与失败回滚。
   - 会话 metadata 中模型、知识包和 Agent 模式恢复。
   - Workspace 切换后的缓存清理。
4. 建立本计划第 7 节的文件级迁移台账。
5. 记录当前构建体积、关键页面加载时间和三档宽度截图基线。
6. 在开发约束中明确历史入口冻结规则。

删除范围：

- 只删除已经证明无引用、无迁移价值的纯残留文件。
- 不删除尚未完成能力判断的历史组件。

退出条件：

- `npm run lint`、`npm test`、`npm run build` 通过。
- 迁移台账覆盖所有生产 JS/JSX 文件。
- 后续 PR 能明确指出替代关系和删除对象。

### 阶段 1：收敛聊天运行基础

目标：先稳定风险最高的运行协议和会话状态，再调整展示层。

工作项：

1. 将 auth、session、stream、SSE 解析和 run 状态逻辑迁入正式目录。
2. 明确状态所有权：
   - 服务端 session metadata 保存会话级配置。
   - StreamManager 保存活动连接与消息缓存。
   - React 页面状态只保存当前展示和临时输入。
3. 验证并保留：
   - queued run admission。
   - cursor resume 和重复事件过滤。
   - 刷新后恢复 queued/running run。
   - awaiting interaction 历史恢复和继续提交。
   - 多会话后台运行和切换。
   - 取消请求与最终状态收敛。
4. 将会话置顶、标题生成、删除和选择逻辑统一到一个 session model。
5. 删除旧入口中重复的流式和会话状态实现。

退出条件：

- 断开 SSE 后恢复不会重复或丢失内容。
- 刷新页面能恢复 queued、running 和 awaiting_input 会话。
- 切换会话和 Workspace 不串消息、不保留旧活动状态。
- 当前生产入口只引用一套 session/stream 实现。

### 阶段 2：加强聊天、Trace 和观测

目标：在当前设计中恢复成熟的消息表达和可观测能力。

工作项：

1. 将消息渲染拆为稳定的领域组件：
   - 用户消息。
   - Agent 正文。
   - reasoning 摘要与详情。
   - 工具调用与工具结果。
   - 子任务和多 Agent 过程。
   - Web 搜索来源。
   - 业务数据查询结果。
   - 错误、取消和等待输入状态。
2. 保持事件原始顺序，不把工具、reasoning 和子任务移动到错误回合。
3. 增加智能滚动：用户主动上滚后不强制回到底部，返回底部后恢复跟随。
4. 增加稳定的复制状态、Clipboard fallback 和反馈失败提示。
5. 统一主消息流与右侧 Trace 的状态来源，避免两套状态推导。
6. 实施 fork Issue #4 和 #10，并补齐总 Issue #3 中的 Trace 完整输出要求：
   - 对话总耗时。
   - 迭代耗时。
   - 工具耗时。
   - 完整工具输出查看入口。
   - 截断提示和安全展示边界。
7. 将会话性能分析、运行详情和本地观测接入正式导航或会话工具栏。
8. 迁移完成后删除旧聊天、DAG、反馈和性能组件。

退出条件：

- 消息顺序与 SSE 事件顺序一致。
- 长工具输出可查看完整内容，并有明确截断状态。
- 用户上滚阅读时不会被新 token 强制拉回底部。
- 反馈、复制、等待输入和错误状态均有可见结果。
- 历史聊天组件及其专属样式已删除。

### 阶段 3：加强知识包、图谱和 Evolution

目标：保留 Meta-wise 知识包工作台，并补齐 Onto Agent 已验证的运行态和进化能力。

工作项：

1. 保留当前文件、版本、上传、下载、启停、激活版本和在线编辑能力。
2. 完善导入冲突处理：
   - 覆盖匹配版本。
   - 覆盖当前激活版本。
   - 创建新版本。
   - 明确取消和错误恢复。
3. 将安全确认抽为可测试的导入流程，不继续保留未使用的 helper。
4. 本体图增加：
   - Trace envelope 到节点/边命中的纯逻辑转换。
   - 运行中高亮与历史命中区分。
   - 固定节点比例。
   - 关系图例。
   - 视图重置。
   - 全屏和 Escape 关闭。
5. Skill Evolution 增加：
   - 按本体分组和折叠。
   - 活动任务轮询和 Workspace 切换清理。
   - 终态任务删除。
   - 行级和词级 Diff。
   - 大段未变化内容折叠。
   - drift 状态下的审批保护与可用拒绝路径。
   - 新版本与覆盖模式审批。
6. Operation Evolution 保留建议、证据、暂存补丁和审批语义，补齐缺失操作。
7. 删除旧 Ontology 和 Evolution 页面及专属工具函数。

退出条件：

- 历史版本保持只读，激活版本操作边界清晰。
- 图谱运行态与当前会话 Trace 一致。
- Workspace 切换后不显示上一个空间的进化任务和 Diff。
- Evolution 所有终态和审批操作具备成功、失败反馈。
- 旧知识和进化页面已删除。

### 阶段 4：加强配置、管理和权限

目标：在三级配置结构中补齐运维能力，并删除历史配置页面。

工作项：

1. 系统配置增加：
   - 配置模板下载。
   - JSON 导入预览和覆盖确认。
   - 分模块连接测试。
   - 上次测试结果和时间。
   - 保存失败的字段级或模块级提示。
2. 模型配置增加：
   - 未保存配置直接测试。
   - 全局和 Workspace 私有模型删除。
   - main/task 默认引用一致性检查。
   - 删除被引用模型时的明确阻止或重绑定流程。
3. Workspace 管理增加搜索、分页、刷新和编辑结果反馈。
4. 用户管理保留创建、重置密码、删除和当前账号保护，并遵循 capability 权限。
5. MCP 管理和强制改密迁入正式 feature，统一视觉和错误反馈。
6. 统一所有配置页面的 loading、empty、read-only、error、success 状态。
7. 删除历史配置、用户、Workspace 和观测组件。

退出条件：

- 普通用户不能通过 UI 触发越权写操作。
- 全局、Workspace、个人作用域切换不会串数据。
- 密钥字段不回显，留空保留语义一致。
- 配置导入前能看到覆盖范围，失败不会污染当前编辑状态。
- 历史配置和管理组件已删除。

### 阶段 5：最终物理收敛

目标：完成目录、入口、样式和依赖树的最终收口。

工作项：

1. 将 `RedesignApp` 改为正式 `app/App`。
2. 将 `redesign/**` 分拆迁入 `app/features/shared`。
3. 删除 `src/App.jsx` 和 `main.jsx` 中的手工回退说明。
4. 删除所有只服务历史实现的组件、utils、样式、资源和测试。
5. 将 `redesign.css` 按 app、shared、feature 拆分，清除无引用 selector。
6. 增加入口可达性检查，CI 中禁止新增生产孤儿模块。
7. 增加 import 边界：
   - feature 不能反向依赖 app。
   - shared 不能依赖具体 feature。
   - feature 之间通过明确接口协作。
8. 更新开发文档、目录说明和前端命令。

退出条件：

- 仓库中不存在第二套前端入口。
- 不存在 `redesign`、`legacy`、`old` 等生产历史目录。
- 入口依赖检查不存在未批准的生产孤儿模块。
- 同一页面和业务能力只有一个实现。
- fork Issue #3 中可由本仓库和受控本地环境验收的前端项完成；依赖真实外部服务的成功链路单独留痕。

## 9. PR 拆分建议

按功能拆分，避免一个超大 PR：

1. `frontend-baseline`：依赖清单、测试命令、运行协议回归基线。
2. `frontend-chat-runtime`：session、stream、resume、awaiting interaction 收敛。
3. `frontend-chat-observability`：消息、Trace、fork Issue #4/#10、性能和本地观测。
4. `frontend-knowledge-evolution`：知识包、图谱运行态和 Evolution。
5. `frontend-settings-admin`：系统配置、模型、Workspace、用户和 MCP。
6. `frontend-final-convergence`：正式目录、旧入口和残留代码清理。

若单个 PR 超过可审查范围，可以继续按子能力拆分，但必须遵守：替代实现和对应删除保持在同一个 PR。

## 10. 测试与验收

### 10.1 自动检查

每个 PR 至少执行：

```bash
cd consumer/frontend
npm run lint
npm test
npm run build
```

测试重点：

- SSE cursor、重复事件和断线恢复。
- queued/running/awaiting/completed/error/cancelled 状态转换。
- 会话切换、Workspace 切换和缓存隔离。
- 权限和只读状态。
- 模型引用与配置 payload。
- 本体导入冲突决策。
- 图谱运行态转换。
- Evolution 分组、Diff 和漂移状态。
- Workspace 搜索与分页。

### 10.2 人工功能验收

1. 登录、登出、首次改密和账号安全。
2. 新建、切换、置顶、删除和搜索会话。
3. 多会话并行运行、停止、刷新恢复和等待输入继续。
4. 模型、知识包和 Agent 模式锁定。
5. 工具、reasoning、子任务、搜索来源和错误消息展示。
6. Trace 与主消息流的一致性。
7. 知识包导入、版本、文件、数据源和图谱。
8. Operation/Skill Evolution 的完整生命周期。
9. 全局、Workspace、个人配置和只读权限。
10. 本地观测、会话性能和运行详情。

### 10.3 UI 验收

至少覆盖：

- 1440px 桌面宽屏。
- 1024px 窄桌面/平板横屏。
- 768px 移动/窄屏。
- 浅色、深色和跟随系统主题。
- 键盘 Tab、Enter、Escape 和焦点恢复。
- loading、empty、error、read-only、disabled、success 状态。

### 10.4 性能门槛

- 阶段 0 记录构建体积和关键页面加载基线。
- 单个阶段的入口构建体积和关键页面加载时间不得无解释退化超过 10%。
- 大型知识、Evolution 和观测页面继续使用按需加载。
- 长会话不得因每个 token 对全量消息做高成本重新计算。
- Workspace 和会话切换不得出现明显空白闪烁或旧数据短暂回显。

2026-07-16 最终复测结果：入口 JS 540.57 kB、gzip 170.13 kB，相对阶段 4 基线分别增长 2.86% 和 2.93%；三次页面导航的 `responseStart`、`DOMContentLoaded`、`load` 中位数分别为 41.9 ms、89.7 ms、90.6 ms，相对阶段 0 基线分别变化 -1.2%、-15.3%、-16.0%，均满足 10% 门槛。

## 11. 完成标准

- [x] `main.jsx` 只挂载正式 `app/App`。
- [x] 历史 `src/App.jsx` 已删除。
- [x] `src/redesign` 已迁移并删除。
- [x] 历史聊天、知识、进化、配置、管理和观测页面已删除。
- [x] 当前运行使用的 hooks、utils、MCP 和改密组件已迁入正式目录。
- [x] 入口依赖扫描不存在未批准的生产孤儿模块。
- [x] 同一页面和业务能力不存在两套生产实现。
- [x] Onto Agent 选定的前端行为、状态语义和回归测试已在 Meta-wise 设计中完成迁移。
- [x] `npm run lint`、`npm test`、`npm run build` 和 Bundle 体积门槛通过。
- [x] GitCode workflow 已接入前端 lint、测试、架构门禁和构建。
- [x] 真实 Chromium 中的功能/UI、确定性写路径、三档响应式与控制台验收已完成，详见[阶段 5 验收记录](./frontend-stage5-final-convergence.md#62-本地启动与浏览器验收)。
- [x] 关键页面加载时间已在本地真实 Chromium 环境复测，未超过 10% 退化门槛。
- [ ] 真实外部模型对话、多会话运行与等待输入成功链路，需可用模型凭据和服务环境。
- [ ] NL2SQL/真实业务数据库查询成功链路，需可用数据库连接与业务数据。
- [ ] Operation/Skill Evolution 的真实生成、审批和落盘成功链路，需可用模型与任务运行环境。
- [x] 开发文档不再指导切换旧入口。
- [x] 回退方案只依赖 Git/发布版本，不依赖残留代码。

## 12. 风险与控制

### 12.1 误删仍需能力

控制措施：先建立文件和能力台账；只有状态为 `covered` 或 `delete` 的文件才能直接删除，其余必须完成迁移验收。

### 12.2 两套实现继续扩散

控制措施：冻结历史入口；PR 审查拒绝向旧页面增加逻辑；替代与删除必须同 PR。

### 12.3 迁移时改变运行协议

控制措施：阶段 1 先建立 SSE、session、awaiting interaction 回归测试，展示层只消费稳定模型。

### 12.4 Workspace 状态串扰

控制措施：所有请求和状态明确绑定 Workspace；切换时取消请求、清缓存、重置选择和未保存草稿。

### 12.5 CSS 互相污染

控制措施：按 app/shared/feature 拆分样式；删除组件时同步删除 selector；不保留历史全局样式兜底。

### 12.6 PR 过大难以审查

控制措施：按垂直能力拆分；每个 PR 有清晰替代关系、删除范围和独立验收记录。

## 13. Issue 管理建议

fork Issue #3 作为总跟踪项，记录：

- 阶段状态。
- 迁移台账链接。
- 各 PR 链接。
- 删除文件统计。
- 自动和人工验收结果。

现有 fork Issue 与阶段的对应关系：

| 阶段 | 关联 Issue |
|---|---|
| 阶段 2：聊天、Trace 和观测 | [#4 对话状态、执行反馈和侧栏布局](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/4)、[#10 会话性能分析和全局性能看板](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/10) |
| 阶段 3：知识包、图谱和 Evolution | [#5 知识包工作台](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/5)、[#6 知识包版本导入](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/6)、[#9 推理过程本体图谱链](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/9)、[#11 本体进化反馈闭环](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/11)、[#12 数据连接与本体数据源收敛](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/12) |
| 阶段 4：配置、管理和权限 | [#7 模型、Langfuse 和平台接入配置](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/7) |
| 跨阶段 UI 质量 | [#8 暗色主题表单可读性与模式控制](https://gitcode.com/manufacture-squad-scene/knowledge-scaffold/issues/8) |

上述关联 Issue 全部完成且最终入口检查通过后，才能关闭总 Issue #3。Trace 完整输出未被子 Issue 完整覆盖的部分，继续由本方案和总 Issue #3 验收。

## 14. 第一批执行项

第一批工作从阶段 0 开始：

1. 生成准确的入口依赖图和不可达文件清单。
2. 将第 7 节初始清单细化到每个生产文件。
3. 增加统一 `npm test` 命令。
4. 补齐 stream/session 的关键回归测试。
5. 确认第一批可直接删除的纯残留文件。
6. 提交第一个只包含基线、测试和明确残留删除的聚焦 PR。

阶段 0 完成前，不开始大规模页面搬迁。
