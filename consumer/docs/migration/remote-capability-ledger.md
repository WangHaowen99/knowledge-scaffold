# 冻结目标能力台账（C01-C30）

> 状态：Phase 0 冻结台账
>
> 目标基线：本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`
> 路径约定：路径均相对 `meta-wise`。本台账记录冻结目标中必须保全的实现和测试，不读取更新后的 `origin/develop`。

## 状态定义

- **存在**：冻结目标已有可执行实现和直接测试证据。
- **部分存在**：已有基础能力，但缺本地 `main` 的最新运行、安全或产品闭环。
- **缺失**：冻结目标没有该能力，必须从本地 `main@31fba9db` 迁入。

## 能力台账

| 编号 | 冻结目标状态 | 关键证据路径 | 测试证据 | 迁移处理 |
|---|---|---|---|---|
| C01 Jiuwen 主 Agent loop | **存在** | `consumer/backend/agentic/loop/jiuwen_adapter.py`、`agentic/loop/jiuwen/`、`agentic/orchestrator.py` | `consumer/backend/tests/test_jiuwen_react_adapter.py`、loop/orchestrator 测试 | 行为保全并迁到 `agent/core/jiuwen_engine.py`；最终只保留 Jiuwen loop。 |
| C02 Jiuwen 子 Agent | **存在** | `agentic/sub_agents/`、`agentic/tools/spawn_sub_agent.py` | sub-agent、spawn orchestration 测试 | 接本地最新 workspace/profile/lineage 和统一 engine。 |
| C03 常驻 event loop 与 warm-up | **存在** | `agentic/loop/jiuwen/common.py`、`agentic/warmup.py` | Jiuwen adapter、并发和 smoke 场景 | 迁到 `agent/core/jiuwen/` 与 `warmup.py`，保留并发锁和无副作用预热。 |
| C04 queued、run replay、SSE cursor | **部分存在** | `agentic/orchestrator.py`、`agentic/routes/workflows.py`、`agentic/stores/` | `test_routes_stream.py`、`test_stream_protocol_v2.py` | 采用本地最新队列、run handle、replay buffer 与 `Last-Event-ID` 契约；Jiuwen 只替换 engine。 |
| C05 durable interaction checkpoint | **部分存在** | `agentic/tools/request_user_interaction.py`、state/store/orchestrator | interaction 与 workflow 测试 | 采用本地持久化 checkpoint、严格响应校验和一次性消费。 |
| C06 严格工具 schema 与安全并发 | **部分存在** | `agentic/tools/base.py`、`executor.py`、`registry.py` | `test_tool_registry.py`、Jiuwen tool tests | 以本地 registry/schema/barrier/terminal batch 为唯一实现，适配 Jiuwen ToolCard。 |
| C07 独立 auth-server | **存在** | `auth-server/auth_server/`、`consumer/backend/agentic/services/remote_auth.py`、`agentic/routes/auth.py` | `auth-server/tests/test_auth_server.py`、`test_remote_auth.py`、`test_auth_register_proxy.py` | 原服务和对外契约保全；consumer 迁到 `api/auth` 与 BFF route。 |
| C08 workspace owner/member | **存在** | `agentic/workspaces.py`、`agentic/routes/workspaces.py` | `test_workspace_authz.py` | 与本地最新存储隔离做并集；ACL 必须在选择用户存储前执行。 |
| C09 session/run 存储边界 | **部分存在** | `agentic/stores/`、workflows/sessions/ACP 路由 | `test_workspace_authz.py`、session/run store 测试 | 采用本地 user+workspace 隔离和标识安全，保留远端 owner/member 裁决。 |
| C10 Neo4j/HugeGraph Graph Provider | **存在** | `agentic/business_data/providers/graph/` | `test_graph_*`、`test_neo4j_graph_backend.py`、`test_hugegraph_graph_backend.py` | 全量迁到 `agent/tools/business_data/providers/graph/`，保留 scope、sanitize 和 14 工具。 |
| C11 Jiuwen GraphQueryAgent | **存在** | `graph/jiuwen_agent.py`、`graph/agent.py`、`graph/runtime.py` | `test_graph_agent.py`、`test_graph_search_runtime.py` | 复用统一 Jiuwen runtime/profile；图工具保持私有。 |
| C12 DataAgent/MindsDB/NL2SQL | **存在，版本较旧** | `agentic/business_data/providers/` | `test_dataagent_provider.py`、MindsDB/NL2SQL 测试 | 以本地 provider 为底，保留远端重试、artifact fallback 和结果归一化。 |
| C13 共享连接与本体直连 | **部分存在** | `agentic/business_data/connections_store.py`、`bindings_store.py`、routes | `test_business_data_connections.py` | 与本地 `ontology-data-sources.json` 合并成 `direct_provider | connection_ref` 单一真相。 |
| C14 独立 MCP search/chat | **存在** | `agentic/mcp/`、`agentic/routes/mcp.py`、`config/mcp_services.yaml` | `test_mcp_search_tools.py`、`test_mcp_transport_mounting.py` | 迁到 `api/protocols/mcp` 与 `integration/mcp`；chat 必须走统一 workflow queue。 |
| C15 MCP Token 与管理 UI | **存在** | `agentic/mcp/token_*`、`service_catalog.py`、Redesign L2 配置 | `test_mcp_token_service.py` | 保留 scope/过期/吊销/last-used；明文仅创建响应显示一次。 |
| C16 Trace 树父子关系 | **存在** | `agentic/loop/jiuwen/trace.py`、Graph trace、streaming、Redesign Trace | `test_graph_provider_trace_parent.py`、stream/trace tests | 与本地 observability 合并，实时、历史和 replay 使用同一节点 key。 |
| C17 W3C traceparent | **存在基础链路** | workflows、observability context、Jiuwen/Graph trace | trace 与 observability tests | 补本地严格格式校验，并贯通 LLM、工具、MCP chat。 |
| C18 知识包树/预览/版本/下载 | **存在** | `agentic/routes/ontologies.py`、`frontend/src/redesign/KnowledgeWorkbench.jsx`、`useKbBrowser.js` | `test_ontology_routes.py` | 在本地 ontology 安全实现上增量接入，保留 `/tree`、`/raw-file` 和版本契约。 |
| C19 v0.1 知识包读取 | **存在** | `agentic/utils/ontology_catalog.py`、ontology import 路由 | `test_tool_registry.py` 与 ontology import 场景 | 在 import 边界归一为当前内部模型，不保留双 runtime schema。 |
| C20 用户记忆、operation/skill evolution | **部分存在** | `agentic/memory/`、`agentic/evolution/`、skill/operation routes | memory/evolution tests | 采用本地最新实现和审批/版本门禁，保留远端可达入口。 |
| C21 本体结构图 | **缺失本地最新闭环** | 冻结目标的 Graph Provider 是业务实例图，不等于本体结构图 | 无本地最新结构图专项测试 | 从本地 `ontology/ontology_graph.py`、API 和前端迁入，保持与 Graph Provider 分离。 |
| C22 role-bound/workspace 私有模型 | **部分存在** | `agentic/config/`、LLM services、Redesign 配置中心 | `test_workspace_runtime_config.py`、LLM/config tests | 采用本地唯一 profile resolver 和 main/task 继承，接入 Jiuwen。 |
| C23 workspace observability/性能分析 | **部分存在** | `agentic/observability/`、`agentic/perf_analysis/` | `test_observability_architecture.py`、perf tests | 采用本地 workspace 配置与路由，保留远端 Trace 字段。 |
| C24 Redesign 视觉与三层配置 IA | **存在** | `frontend/src/redesign/RedesignApp.jsx`、`AppShell.jsx`、`ConfigCenter.jsx`、`theme.js`、`redesign.css` | frontend build 与后续截图验收 | 作为唯一活动外壳；本地新增页面按同一 token/组件重做。 |
| C25 会话置顶、恢复、配置锁定 | **部分存在** | `redesign/ChatHistorySidebar.jsx`、`ChatWorkspace.jsx`、现有 hooks | stream/session 前端 tests | 迁入本地最新 pin、cursor 恢复、awaiting-input 和会话配置锁定。 |
| C26 A2A/ACP | **存在较旧实现** | `agentic/a2a/`、`agentic/acp/`、routes | A2A/ACP tests | 采用本地最新协议层，复用统一 Jiuwen orchestrator 和 workspace authz。 |
| C27 本体/技能/脚本工具链 | **部分存在** | `skill_parser/`、`agentic/tools/ontology_context.py`、`skill.py`、`execute_script.py` | parser/tool/ontology tests | 以本地安全实现为底，保留远端知识包版本与工作台契约。 |
| C28 Web Search 与 onto-platform | **部分存在** | `agentic/web_search/`、`agentic/tools/web_search.py` | web search tests | 保留搜索能力，迁入本地 `integration/platform` 和错误契约。 |
| C29 Ops 配额、日志与标识安全 | **部分存在** | `agentic/config/ops_config.py`、logging、stores、安全工具 | logging/config/security tests | 采用本地最新配额、保留策略和标识防碰撞实现。 |
| C30 SDK 与 business-frontend 公共契约 | **部分存在** | `consumer/sdk/`、HTTP/SSE routes | SDK/route contract tests | SDK 与 schema 做并集；`business-frontend` 不并入 Redesign，只验证公共契约。 |

## 冻结基线排除项

执行开始前曾观察到本地 remote-tracking ref 指向更新的 `origin/develop@40bb77a`。用户随后明确要求以当前本地 `develop@1d07c1ee` 为基线并停止拉取远端。因此 `1d07c1ee..40bb77a` 的 auth 注册上限改动及其测试不属于本次能力台账，也不得通过后续 fetch/pull 混入。

## 执行结论

1. C01-C03、C07-C08、C10-C11、C14-C19、C24 是冻结目标的重点保全范围。
2. C04-C06、C09、C12-C13、C20、C22-C23、C25-C30 采用两边并集，运行与安全底座使用本地 `main` 最新实现。
3. C21 从本地迁入；它与 Neo4j/HugeGraph 业务实例图并存，API 和数据语义必须分开。
4. 任何目标独有测试在新路径尚未通过前，都不能删除旧测试或标记能力完成。
