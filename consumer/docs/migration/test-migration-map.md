# 远端测试迁移映射

> 冻结目标：本地 `develop@1d07c1ee4905b89843b5158e5bbedd91fa3b9537`
>
> 本地源码：`main@31fba9db4bd7103f4d3ab54da8f3031e50cef4d2`
> 规则：保留原测试场景和测试函数名，只修改 import、fixture 与新契约必须变化的断言；禁止用 `skip` 隐藏迁移失败。

## 已复现冻结基线

执行证据见 `baseline-test-report.md`：

- `auth-server/tests`：32/32 通过。
- `test_remote_auth.py + test_auth_register_proxy.py + test_workspace_authz.py`：47/47 通过。

## 文件与阶段映射

| 领域 | 冻结目标测试 | 目标测试路径 | Phase | 类型 | 迁移后验收 |
|---|---|---|---:|---|---|
| auth-server 核心 | `auth-server/tests/test_auth_server.py` | 原路径保留 | 2 | 纯单元 + Flask test client；密钥/文件使用临时目录 | `cd auth-server && .venv/bin/python -m pytest tests -q` |
| consumer 认证契约 | `test_auth_contract.py`、`test_remote_auth.py`、`test_auth_register_proxy.py`、`test_auth_limits.py`、`test_auth_security.py` | `consumer/backend/tests/` 原文件名保留，import 改为 `api.auth`/`api.routes.auth` | 2 | 单元 + 本机回环临时 auth-server | 运行五个文件；回环监听权限必须可用 |
| workspace authz | `test_workspace_authz.py`、`test_workspace_runtime_config.py` | 原文件名保留，import 改为 `workspace`/`api.routes.workspaces` | 2 | 纯单元/Flask client | owner/member、公共空间、ACL、runtime config 全部通过 |
| Jiuwen 主 loop | `test_jiuwen_react_adapter.py`、`test_loop_shared.py` | 原文件名保留，import 改为 `agent.core.jiuwen*` | 3 | 纯单元 + fake LLM/runtime | Jiuwen adapter、stream、reasoning、tool 回写、异常契约通过 |
| Jiuwen 子 Agent | `test_spawn_sub_agent_orchestration.py`、sub-agent/manager tests | 原文件名保留，目标 import 为 `agent.sub_agents` | 3 | 纯单元/模拟 LLM | workspace/profile/lineage、工具暴露与取消传播通过 |
| Jiuwen smoke/压力 | `agentic/loop/jiuwen/smoke.py` 与并发场景 | `agent/core/jiuwen/smoke.py` + 压力脚本 | 3 | 本地 runtime；可使用 mock LLM | smoke 通过；20 并发 run、500 短 run 无 event-loop/resource 泄漏 |
| Graph scope/mapping | `test_graph_scope.py`、`test_graph_filter_expr.py`、`test_graph_mapping_resolver.py`、`test_graph_scope_migration_script.py` | 原文件名保留，import 改为 `agent.tools.business_data.providers.graph` | 4 | 纯单元 | 标识符、scope、mapping 顺序、迁移幂等通过 |
| Graph runtime/tools | `test_graph_provider.py`、`test_graph_search_runtime.py`、`test_graph_tools.py`、`test_graph_agent.py` | 原文件名保留 | 4 | fake backend/fake LLM | 14 工具、批量查询、去重、截断、重试、超时通过 |
| Neo4j/HugeGraph adapter | `test_neo4j_graph_backend.py`、`test_hugegraph_graph_backend.py` | 原文件名保留 | 4 | 模拟 driver/client；不等于真实服务 E2E | 两个 adapter 单测通过；真实服务另按门禁执行 |
| DataAgent 与连接 | `test_dataagent_provider.py`、`test_business_data_connections.py`、`test_business_data_search_resolution.py`、`test_business_data_search_sse.py` | 原文件名保留，import 改为 `agent.tools.business_data` | 4 | mock HTTP/provider | retry、artifact fallback、两种连接模式、SSE 错误契约通过 |
| 知识包 | `test_ontology_routes.py`、`test_ontologies.py`、`test_skill_ontology_parser.py` | 原文件名保留，import 改为 `ontology`/`api.routes.ontologies` | 4 | 纯单元/临时文件系统 | v0.1、当前格式、树、raw-file、版本、下载、路径安全通过 |
| MCP search | `test_mcp_search_tools.py` | 原文件名保留，import 改为 `api.protocols.mcp`/Graph runtime | 5 | 纯单元/fake Graph | 14 个确定性工具、scope、sanitize 通过 |
| MCP Token | `test_mcp_token_service.py` | 原文件名保留，import 改为 `integration.mcp` | 5 | 纯单元/临时存储 | 单次明文、hash/mask、last-used、过期、吊销和迁移通过 |
| MCP transport | `test_mcp_transport_mounting.py` | 原文件名保留，import 改为 `api.protocols.mcp.server` | 5 | 本地 transport 装载；真实 7488 另测 | Streamable HTTP/SSE 四端点挂载与 health 通过 |
| Trace 父子关系 | `test_graph_provider_trace_parent.py`、`test_direct_execution_trace.py`、`test_tracing.py` | 原文件名保留，import 改为 `integration.observability` 与新 agent 路径 | 6 | 纯单元 | span_key/parent_key、iteration/tool/Graph parent、lineage 通过 |
| Trace transport/replay | `test_routes_stream.py`、`test_stream_protocol_v2.py`、`test_observability_architecture.py` | 原文件名保留 | 6 | Flask client/模拟 exporter | 实时与历史 key 一致；断线重放不重复、不换父节点 |

## 测试函数名与场景保全规则

1. 每个上表文件先用冻结基线收集 node id，再在目标新结构收集；缺失 node id 必须在迁移说明中逐项解释。
2. 只允许因模块路径变化改 import；断言变化必须对应已经冻结的新契约，并在 commit 中说明。
3. 需要 Neo4j、HugeGraph、DataAgent、LLM、Langfuse 或 7488 实进程的测试标记为“真实环境门禁”，不能改成无条件 skip。
4. 模拟 driver、fake LLM、Flask test client 和本机回环临时服务仍属于自动化测试，不替代真实服务矩阵。
5. 当前源码已有的可选 fixture skip 单独记录在 `baseline-test-report.md`；不得把迁移失败归入该例外。

## 分阶段命令

```bash
# Phase 2
cd auth-server && .venv/bin/python -m pytest tests -q
cd ../consumer && PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_auth_contract.py backend/tests/test_remote_auth.py \
  backend/tests/test_auth_register_proxy.py backend/tests/test_auth_limits.py \
  backend/tests/test_auth_security.py backend/tests/test_workspace_authz.py -q

# Phase 3
cd consumer && PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_jiuwen_react_adapter.py \
  backend/tests/test_spawn_sub_agent_orchestration.py \
  backend/tests/test_tool_registry.py backend/tests/test_universal_prompt.py -q

# Phase 4
cd consumer && PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_dataagent_provider.py backend/tests/test_business_data_connections.py \
  backend/tests/test_graph_scope.py backend/tests/test_graph_mapping_resolver.py \
  backend/tests/test_graph_provider.py backend/tests/test_graph_tools.py \
  backend/tests/test_neo4j_graph_backend.py backend/tests/test_hugegraph_graph_backend.py \
  backend/tests/test_ontology_routes.py -q

# Phase 5-6
cd consumer && PYTHONPATH=backend ./venv/bin/python -m pytest \
  backend/tests/test_mcp_search_tools.py backend/tests/test_mcp_token_service.py \
  backend/tests/test_mcp_transport_mounting.py \
  backend/tests/test_graph_provider_trace_parent.py backend/tests/test_routes_stream.py \
  backend/tests/test_stream_protocol_v2.py -q
```

## 真实外部服务门禁

以下内容在收到对应环境后执行，Phase 0 只冻结场景，不伪造通过结果：

- Neo4j 与 HugeGraph 的同一 14 工具矩阵及跨 workspace 隔离。
- MCP 7488 health、四个 transport endpoint、token 重启持久化与 chat run owner。
- DataAgent/MindsDB/NL2SQL 的真实请求、重试和密钥脱敏。
- Jiuwen 使用真实 LLM 的 quick/thinking/multi-agent、取消和并发压力。
- W3C traceparent 进入真实 Langfuse/外部 collector 后的父子链。
