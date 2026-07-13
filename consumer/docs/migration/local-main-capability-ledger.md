# 本地 main 能力台账（C01-C30）

> 状态：Phase 0 冻结台账
>
> 本地源码快照：`/tmp/onto-agent-main-source-31fba9db`，`31fba9db4bd7103f4d3ab54da8f3031e50cef4d2`
>
> 迁入目标基线：`meta-wise develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`
> 路径约定：来源路径相对本地源码快照；测试路径相对快照；目标路径相对 `meta-wise`。本台账只判断本地 `main`，远端能力以“缺失”明确标记。

## 状态定义

- **存在**：本地有可迁移实现，并有对应自动化测试或契约测试。
- **部分存在**：本地有基础实现，但缺少 Cxx 定义中的远端扩展或完整产品闭环。
- **缺失**：本地没有该能力，迁移时必须从目标基线保全，不能被本地文件覆盖。

## 能力台账

| 编号 | 本地状态 | 本地关键来源路径 | 本地对应测试/证据 | 迁入 meta-wise 的目标路径 | 与远端冲突或迁移风险 |
|---|---|---|---|---|---|
| C01 Jiuwen 主 Agent loop | **缺失**。本地为 native engine/orchestrator | `backend/agent/core/engine.py`；`backend/agent/core/orchestrator.py` | `backend/tests/` 中 orchestrator、workflow、tool-loop 契约 | `consumer/backend/agent/core/engine.py`；`consumer/backend/agent/core/jiuwen_engine.py`；`consumer/backend/agent/core/jiuwen/` | 远端 Jiuwen 是最终唯一 loop。本地 orchestrator 的队列、状态和安全边界必须保留；禁止保留双 loop 或静默 fallback。 |
| C02 Jiuwen 子 Agent | **缺失** Jiuwen 子 Agent；本地有通用 sub-agent 能力 | `backend/agent/sub_agents/`；`backend/agent/core/orchestrator.py` | `backend/tests/` 中 sub-agent、workspace/profile 传播测试 | `consumer/backend/agent/sub_agents/`；`consumer/backend/agent/core/jiuwen/` | 远端 Jiuwen 调用模型与本地 sub-agent 状态模型不同，必须统一 workspace、model profile、trace 和工具权限传播。 |
| C03 Jiuwen 常驻 event loop 和 warm-up | **缺失** | 本地仅有 `backend/app.py` 启动流程及 native engine 生命周期 | `backend/tests/` 中 app startup、并发 orchestrator 测试 | `consumer/backend/agent/core/warmup.py`；`consumer/backend/agent/core/jiuwen/`；`consumer/backend/app.py` | 采用远端实现时需补并发与重复预热测试；预热不得注册重复工具或污染 workspace。 |
| C04 queued、run replay、SSE cursor | **存在** | `backend/api/routes/workflows.py`；`backend/agent/core/orchestrator.py`；`backend/persistence/` | `backend/tests/` 中 workflow queue、run replay、SSE resume/`Last-Event-ID` 测试 | `consumer/backend/api/routes/workflows.py`；`consumer/backend/agent/core/orchestrator.py`；`consumer/backend/persistence/` | 远端旧 orchestrator/transport 可能覆盖 queued 与 cursor 语义。Jiuwen 只能接入统一 engine 门面，不能重建第二套 run/store。 |
| C05 durable interaction checkpoint | **存在** | `backend/agent/core/orchestrator.py`；`backend/agent/state.py`；`backend/persistence/`；`frontend/src/hooks/` | `backend/tests/` 中 `request_user_interaction`、checkpoint/resume 测试；前端 hook 测试 | `consumer/backend/agent/core/orchestrator.py`；`consumer/backend/agent/state.py`；`consumer/backend/persistence/`；`consumer/frontend/src/hooks/` | 远端 Jiuwen state/event 类型需映射到 durable checkpoint。必须保持 input/select/approval 单次消费和重启后恢复。 |
| C06 严格工具 schema 和安全并发工具 | **存在** | `backend/agent/tools/`；`backend/agent/core/engine.py` | `backend/tests/` 中 tool schema、并发 barrier、terminal batch、参数校验测试 | `consumer/backend/agent/tools/`；`consumer/backend/agent/core/jiuwen_engine.py` | Jiuwen adapter 可能放宽 JSON schema 或改变 batch/terminal 顺序；必须由本地 registry 和校验器统一约束。 |
| C07 独立 auth-server | **缺失**。本地没有根级独立身份服务 | 本地仅有 consumer 内认证/调用方约束，位于 `backend/api/auth/`、`backend/api/routes/` | 本地认证与 route auth 测试不能替代独立 auth-server 测试 | 保留 `auth-server/`；接入 `consumer/backend/api/auth/`、`consumer/backend/api/routes/auth.py` | 远端 `auth-server` 是唯一身份源，禁止用本地认证文件覆盖。consumer 只保留 BFF、JWKS 验签和授权裁决。 |
| C08 workspace owner/member | **部分存在**。有 workspace 与调用方隔离，缺远端完整 owner/member 管理闭环 | `backend/workspace/`；`backend/api/routes/workspaces.py`；`backend/persistence/` | `backend/tests/` 中 workspace 隔离、allowed-caller、跨 workspace 拒绝测试 | `consumer/backend/workspace/`；`consumer/backend/api/routes/workspaces.py` | 远端成员模型、路由和 UI 必须保留；合并时不能把 owner/member 降级为本地单用户或 allowed-callers 规则。 |
| C09 session/run 存储边界 | **存在** | `backend/workspace/`；`backend/persistence/`；`backend/api/routes/workflows.py`；`backend/api/routes/sessions.py` | `backend/tests/` 中跨 user/workspace session、run 访问和续跑拒绝测试 | 同名迁入 `consumer/backend/workspace/`、`persistence/`、`api/routes/` | 远端 store key/owner 字段与本地标识编码可能不同；必须先迁数据，再强制 user+workspace 双重 scope。 |
| C10 Neo4j/HugeGraph Graph Provider | **缺失** | 无 Graph Provider；本地 business-data 仅覆盖现有数据 provider | 本地无 14 类图工具及 Neo4j/HugeGraph scope/sanitize 全套测试 | `consumer/backend/agent/tools/business_data/providers/graph/` | 必须保全远端 provider、图查询 sanitize 和 workspace scope；不得让图连接绕过统一数据源模型。 |
| C11 Jiuwen GraphQueryAgent | **缺失** | 无 | 无私有图工具、重试、去重、超时契约测试 | `consumer/backend/agent/tools/business_data/providers/graph/`；`consumer/backend/agent/core/jiuwen/` | 完整采用远端能力并接本地 LLM/workspace/trace；禁止 Graph Agent 自建模型配置或工具 registry。 |
| C12 DataAgent/MindsDB/NL2SQL | **存在本地主体，远端扩展待并集** | `backend/agent/tools/business_data/`；`backend/agent/tools/business_data/providers/`；`backend/config/` | `backend/tests/` 中 DataAgent、MindsDB、NL2SQL provider、结果归一化和错误契约测试 | `consumer/backend/agent/tools/business_data/`；`consumer/backend/config/` | 两边 provider 命名、重试和结果结构存在差异；以本地实现为底，补远端 DataAgent 行为，禁止重复 provider。 |
| C13 共享连接 + 本体直连 | **部分存在**。本地具备本体直连和 provider 配置，缺远端共享连接完整 UI/引用链 | `backend/ontology/`；`backend/agent/tools/business_data/`；`backend/config/`；`frontend/src/` 数据源配置 | `backend/tests/` 中 ontology data source、provider config/resolve 测试 | `consumer/backend/ontology/`；`consumer/backend/agent/tools/business_data/`；`consumer/frontend/src/redesign/` | 需统一为 `direct_provider | connection_ref` 单一模型。迁移脚本必须消除双份配置真相并校验 workspace ownership。 |
| C14 独立 MCP search/chat 服务 | **缺失** | 本地有通用工具/协议基础，但没有远端独立 7488 MCP search/chat 服务 | 无远端两类 transport、14 search tools、chat 全套测试 | `consumer/backend/api/protocols/mcp/`；`consumer/backend/integration/mcp/` | 必须保全远端服务契约，但 chat 必须进入统一 workflow/run API；禁止独立 MCP 进程自建 orchestrator。 |
| C15 MCP Token 与管理 UI | **缺失** | 无远端 MCP token store/catalog 和 Redesign 管理页 | 无创建时单次显示、last-used、过期、吊销全套测试 | `consumer/backend/integration/mcp/`；`consumer/frontend/src/redesign/` | Token 必须按 workspace scope 强制，secret 不得落日志；采用远端 UI 时不得暴露可再次读取的明文 token。 |
| C16 Trace 树父子关系 | **部分存在**。本地有 observability/运行详情，缺远端 `span_key/parent_key` 完整契约 | `backend/integration/observability/`；`backend/api/routes/workflows.py`；`frontend/src/` 运行详情 | `backend/tests/` 中 observability、trace/运行事件测试 | `consumer/backend/integration/observability/`；`consumer/backend/api/routes/workflows.py`；`consumer/frontend/src/redesign/` | 需合并远端父子字段与本地事件模型，历史 replay 和实时 SSE 必须产生同一棵树。 |
| C17 W3C traceparent | **缺失远端完整传播链**。本地 observability 不能证明入口至 LLM/工具全链透传 | `backend/integration/observability/`；`backend/api/routes/workflows.py` | 本地 observability 测试；无远端 W3C 全链验收证据 | 同名迁入 `consumer/backend/integration/observability/`、`api/routes/workflows.py` | 必须保全远端入口解析/生成规则，并让 Jiuwen、LLM 与工具 span 继承；无效 header 要安全拒绝或重建。 |
| C18 知识包树、预览、导入、启停、版本、下载 | **部分存在**。本地有 ontology 导入/编辑/运行能力，缺远端知识包工作台完整产品闭环 | `backend/ontology/`；`backend/api/routes/ontologies.py`；`frontend/src/` ontology 页面 | `backend/tests/` 中 ontology import/export、zip 校验、版本/编辑测试 | `consumer/backend/ontology/`；`consumer/backend/api/routes/ontologies.py`；`consumer/frontend/src/redesign/` | 后端以本地安全校验为底，保全远端 API/UI 行为；禁止用本地页面覆盖 Redesign 工作台。 |
| C19 v0.1 知识包读取 | **缺失远端兼容能力** | 本地 ontology loader/importer 仅作为当前内部模型基础 | 本地 ontology import 测试；无 v0.1 归一化专项证据 | `consumer/backend/ontology/` | 远端 v0.1 reader 必须迁入 importer 边界并归一化到当前模型；禁止把旧 schema 扩散到 runtime。 |
| C20 用户记忆、operation/skill evolution | **存在** | `backend/agent/memory/`；`backend/agent/evolution/`；`backend/agent/skill_evolution/`；相关 `api/routes/`；`frontend/src/` | `backend/tests/` 中 memory CRUD、evolution diff/approval/version gate 测试；前端页面测试 | `consumer/backend/agent/memory/`；`agent/evolution/`；`agent/skill_evolution/`；`api/routes/`；`consumer/frontend/src/redesign/` | 后端能力直接迁入；前端必须按远端 token/组件重做，审批版本门禁不能因 Jiuwen 接入而绕过。 |
| C21 本体结构图 | **存在** | `backend/ontology/`；`backend/api/routes/ontologies.py`；`frontend/src/` ontology graph 页面/组件 | `backend/tests/` 中 ontology graph API/数据加载测试；前端构建契约 | `consumer/backend/ontology/`；`consumer/backend/api/routes/ontologies.py`；`consumer/frontend/src/redesign/` | API 可迁入，页面需按 Redesign 重做；大图加载、全屏和 workspace scope 都要保留。 |
| C22 role-bound/workspace 私有模型 | **存在** | `backend/config/`；`backend/llm/`；`backend/workspace/`；`frontend/src/` 模型配置页面 | `backend/tests/` 中 model profile、role binding、workspace private、main/task 继承测试 | `consumer/backend/config/`；`consumer/backend/llm/`；`consumer/backend/workspace/`；`consumer/frontend/src/redesign/` | 远端 Jiuwen/profile 配置可能另有模型入口；必须只保留一套 resolver，并在后端强制 workspace ownership。 |
| C23 workspace observability、性能分析 | **存在** | `backend/integration/observability/`；`backend/utils/perf_analysis/`；相关 `api/routes/`；`frontend/src/` | `backend/tests/` 中 observability config、连接测试、perf analysis、run detail 测试 | `consumer/backend/integration/observability/`；`consumer/backend/utils/perf_analysis/`；`consumer/frontend/src/redesign/` | 合并 C16/C17 远端 trace 字段；连接凭据必须留在后端，页面按 Redesign 重做。 |
| C24 远端控制台视觉与三层配置 IA | **缺失** | 本地 `frontend/src/` 是另一套外壳和信息架构 | 本地前端 lint/build 只能证明自身可用，不能替代 Redesign 视觉验收 | 保留 `consumer/frontend/src/redesign/`，入口继续由 `consumer/frontend/src/main.jsx` 进入 `RedesignApp` | 远端视觉体系是唯一活动外壳。本地页面只迁运行逻辑并重做 UI，禁止切回本地 App 或复制两套导航。 |
| C25 会话置顶、恢复、锁定配置 | **存在** | `frontend/src/hooks/`；`frontend/src/components/ChatHistory*`；`frontend/src/components/ChatWorkspace*`；`backend/api/routes/workflows.py` | 前端 hooks/session 恢复测试；后端 SSE cursor、checkpoint/replay 测试 | `consumer/frontend/src/hooks/`；`consumer/frontend/src/redesign/`；`consumer/backend/api/routes/workflows.py` | 需把运行逻辑移入远端组件体系；刷新、换页、awaiting input 和 running 状态不得被视觉重做破坏。 |
| C26 A2A、ACP 协议适配 | **存在** | `backend/api/protocols/a2a/`；`backend/api/protocols/acp/`；对应 `backend/api/routes/` | `backend/tests/` 中 A2A/ACP discovery、create、stream、cancel、resume 测试 | `consumer/backend/api/protocols/a2a/`；`consumer/backend/api/protocols/acp/`；`consumer/backend/api/routes/` | 协议层必须复用同一 orchestrator/run store；Jiuwen 接入后不能形成协议专用 loop 或绕过 workspace 权限。 |
| C27 本体/技能/脚本工具链 | **存在** | `backend/ontology/`；`backend/skill_parser/`；`backend/agent/tools/`；`tools/` | `backend/tests/` 中 ontology package/import/edit/assets、skill parser、read ledger、execute script 测试 | `consumer/backend/ontology/`；`consumer/backend/skill_parser/`；`consumer/backend/agent/tools/`；`consumer/tools/` | 远端知识包格式与本地 parser 需在 importer 处归一；脚本执行、路径和 assets 必须维持本地安全边界。 |
| C28 Web Search 与 onto-platform | **存在** | `backend/agent/tools/`；`backend/integration/platform/`；`backend/config/` | `backend/tests/` 中 web search、platform client/config、连接和错误契约测试 | `consumer/backend/agent/tools/`；`consumer/backend/integration/platform/`；`consumer/backend/config/` | Jiuwen tool adapter 必须调用本地统一工具；不得复制配置、绕过 schema 或吞掉平台错误。 |
| C29 Ops 配额、日志与存储标识安全 | **存在** | `backend/config/`；`backend/utils/`；`backend/persistence/`；`backend/workspace/` | `backend/tests/` 中 quota、日志保留、路径穿越、标识碰撞、store scope 测试 | `consumer/backend/config/`；`consumer/backend/utils/`；`consumer/backend/persistence/`；`consumer/backend/workspace/` | 远端 store/config 旧实现可能削弱编码和隔离规则；迁移时以本地安全实现为底，数据 key 变化需显式迁移。 |
| C30 SDK 与 business-frontend 公共契约 | **存在** | `sdk/`；`business-frontend/`；`backend/api/routes/`；`backend/agent/events.py` | `backend/tests/` 与 `sdk/tests/` 的 HTTP/SSE contract；`business-frontend` 构建/调用契约 | `consumer/sdk/`；`consumer/backend/api/routes/`；`consumer/backend/agent/events.py`；外部 `business-frontend` 不并入控制台 | 目录重构和 Jiuwen 事件映射不能改变已有 HTTP/SSE schema。`business-frontend` 保持独立，只验证公共契约不回退。 |

## 本地缺失能力汇总

本地 `main@31fba9db` 明确缺失、必须从冻结目标基线保全的能力为：

- C01 Jiuwen 主 Agent loop。
- C02 Jiuwen 子 Agent 实现。
- C03 Jiuwen 常驻 event loop 与 warm-up。
- C07 根级独立 `auth-server`。
- C10 Neo4j/HugeGraph Graph Provider。
- C11 Jiuwen GraphQueryAgent。
- C14 独立 MCP search/chat 服务。
- C15 MCP Token 与管理 UI。
- C19 v0.1 知识包兼容读取。
- C24 Redesign 控制台视觉体系与三层配置 IA。

C08、C13、C16、C17、C18 为**部分存在**：本地基础能力可迁入，但不能代替远端 owner/member、共享连接、Trace 父子/W3C、知识包工作台等扩展。

## 迁移执行结论

1. C04-C06、C09、C20-C23、C25-C30 以本地实现和安全测试为底迁入目标分层路径。
2. C01-C03、C07、C10-C11、C14-C15、C19、C24 从目标基线原样保全行为，再适配本地分层结构和统一运行时。
3. C12、C13、C16-C18 必须做行为并集，不能用任一侧整文件覆盖。
4. 所有 Agent、A2A、ACP、MCP chat 最终必须进入同一 Jiuwen engine 门面、orchestrator、workspace scope 和 run store。
5. 本台账是 Phase 0 能力映射，不代表测试已经在目标仓迁移后通过；各 Cxx 必须在对应迁移阶段补齐自动化或真实环境验收证据。
