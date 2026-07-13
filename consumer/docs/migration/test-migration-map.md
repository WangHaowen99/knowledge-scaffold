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
| Jiuwen smoke/压力 | `agentic/loop/jiuwen/smoke.py` 与并发场景 | `agent/core/jiuwen/smoke.py`；`scripts/jiuwen_runtime_pressure.py` | 3 | 本地 runtime；离线模型不访问网络 | smoke 注册与释放自动化覆盖；20 并发、500 短 run 已通过，资源与事件循环收口证据见 `jiuwen-runtime-pressure.md` |
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

### `test_orchestrator_runtime.py` 节点去向

Phase 1 以本地 `main@31fba9db` 的 26 个节点为准，完整迁入
`consumer/backend/tests/test_orchestrator_runtime.py`，保留测试函数名并改用
`agent.core.orchestrator`。冻结目标中的三个额外节点处理如下：

| 冻结目标节点 | 去向 | 理由 |
|---|---|---|
| `test_stream_agent_loop_can_use_injected_jiuwen_agent_factory` | `test_stream_agent_loop_emits_custom_before_values`、`test_agent_loop_engine_is_jiuwen_only_facade`、`test_jiuwen_loop_engine_invokes_agent_and_updates_current_state` | Phase 3 改为 orchestrator 只注入稳定 engine 门面，agent factory 注入下沉到 Jiuwen engine 测试；事件、快照和实际 Jiuwen 调用分别覆盖。 |
| `test_public_workflow_error_preserves_input_token_limit_detail` | 显式退役 | M3-1 删除旧 `InputTokenLimitError` 路径；当前公共错误契约保留 LLM rate-limit 明细，并继续屏蔽普通 `ValueError`。 |
| `test_orchestrator_memory_hooks_do_not_require_session_id` | 显式退役 | M3-1 删除 `ov_session_id`、OpenViking 与旧 memory hook；当前 orchestrator 使用 reflection、operation evolution 和新 session 状态契约。 |

### `test_tool_registry.py` 节点去向

Phase 1 保留本地 source 的 56 个节点，并追加冻结目标的 5 个动态 schema、移除旧工具、file tool 和 terminal outcome 节点。其余三个冻结目标节点处理如下：

| 冻结目标节点 | 去向 | 理由 |
|---|---|---|
| `test_execute_tool_batch_accepts_internal_tool_invocation` | 延后至 Phase 3 | 与 Jiuwen adapter 的中立 `ToolInvocation` 边界一起迁移。 |
| `test_execute_tool_batch_rejects_legacy_openai_style_tool_call_dict` | 延后至 Phase 3 | Phase 1 保留本地最新 dict tool-call 契约；Jiuwen 接入时统一转换边界。 |
| `test_registry_loads_v01_ontology_package_resources` | 延后至 Phase 4 | v0.1 只在 package import normalization 边界兼容，运行时 registry 只消费当前 manifest/catalog。 |

### `test_agent_loop.py` 的 34 个 native-internal 节点去向

M3-1 保留 `agent/core/engine.py` 作为 orchestrator 的唯一稳定接口，具体实现只继承
`JiuwenReactLoopEngine`。旧文件中的 34 个节点绑定 native loop 的私有调度器、流式
tool-call 聚合器和计时实现，不能继续作为门面测试，也不使用 `skip` 保留空壳。行为契约
按下表迁入 `test_jiuwen_react_adapter.py`、`test_loop_shared.py` 和
`test_tool_registry.py`；`test_agent_loop.py` 只验证继承接口、共享 mode 解析与 Jiuwen
不可用时直接报错。

| # | 旧 native 节点 | Jiuwen/共享契约去向 | 处理说明 |
|---:|---|---|---|
| 1 | `test_agent_loop_seeds_messages_and_finishes_without_tools` | `test_jiuwen_loop_engine_invokes_agent_and_updates_current_state` | 保留消息初始化、final output、snapshot 和零工具完成事件。 |
| 2 | `test_agent_loop_keeps_existing_system_prompt_locked` | `test_jiuwen_loop_engine_seeds_persisted_history_into_new_context` | Jiuwen session/context 负责历史注入；旧 engine 私有 prompt-lock 分支退役。 |
| 3 | `test_current_date_reminder_appends_to_latest_user_message` | `test_jiuwen_loop_engine_appends_current_date_after_selected_ontology_context` | 日期提醒在 Jiuwen 输入边界验证。 |
| 4 | `test_agent_loop_exposes_web_search_only_when_enabled` | `test_jiuwen_loop_engine_exposes_web_search_only_when_enabled`、`test_registry_hides_web_search_until_enabled_for_schema_exposure` | engine 与 registry 两层同时覆盖工具曝光。 |
| 5 | `test_noop_observer_does_not_materialize_trace_payloads` | `test_jiuwen_loop_engine_registers_current_tools_for_default_agent`、`test_jiuwen_loop_engine_emits_runtime_trace_events` | 保留 `NOOP_OBSERVER` 注入和启用 observer 的事件契约；旧 native payload 构造路径退役。 |
| 6 | `test_agent_loop_finishes_trace_when_setup_fails` | `test_jiuwen_loop_engine_finishes_trace_when_setup_fails` | 原行为保留。 |
| 7 | `test_agent_loop_closes_llm_trace_before_streamed_tool_wait_cancellation` | `test_jiuwen_loop_engine_stops_stream_when_run_is_cancelled`、`test_jiuwen_loop_marks_started_tool_batch_cancelled_when_agent_cancelled` | Jiuwen 没有 native streamed-tool wait；保留流取消与 tool batch 取消状态。 |
| 8 | `test_agent_loop_marks_started_streamed_tool_batch_failed_when_llm_errors` | `test_jiuwen_loop_marks_started_tool_batch_failed_when_agent_errors` | 原行为迁到 Jiuwen callback/trace 边界。 |
| 9 | `test_agent_loop_marks_started_streamed_tool_batch_cancelled_when_llm_cancelled` | `test_jiuwen_loop_marks_started_tool_batch_cancelled_when_agent_cancelled` | 原行为迁到 Jiuwen callback/trace 边界。 |
| 10 | `test_iteration_metrics_include_zero_tool_timing_without_tools` | `test_jiuwen_loop_engine_invokes_agent_and_updates_current_state` | 零工具 iteration 由 Jiuwen 完成事件覆盖；旧 native metric builder 调用点退役。 |
| 11 | `test_agent_loop_passes_sync_request_mode_directly_to_llm` | `test_jiuwen_loop_engine_uses_invoke_when_request_mode_is_sync` | 原行为保留。 |
| 12 | `test_agent_loop_executes_tool_batch_and_continues` | `test_jiuwen_loop_default_path_invokes_registered_tool_and_reports_tool_metrics` | 覆盖本地 executor、tool 回写、continue iteration 和最终输出。 |
| 13 | `test_agent_loop_preserves_reasoning_content_after_tool_call` | `test_current_runtime_tool_adapter_preserves_pending_assistant_context`、`test_jiuwen_loop_engine_preserves_streamed_reasoning_content_on_assistant_message` | reasoning 与 tool-call assistant 上下文均保留。 |
| 14 | `test_agent_loop_preserves_prior_turn_reasoning_content_before_new_user_request` | `test_jiuwen_loop_engine_seeds_persisted_history_into_new_context`、`test_jiuwen_model_trace_stashes_assistant_context_for_tool_calls` | 历史由 Jiuwen context seed，当前轮 reasoning 由 trace bridge 保存。 |
| 15 | `test_agent_loop_executes_closed_stream_tool_calls_without_reexecution` | `test_jiuwen_loop_default_path_invokes_registered_tool_and_reports_tool_metrics`、`test_current_runtime_tool_adapter_uses_unique_tool_call_ids_per_invocation` | native closed-call 聚合器退役；Jiuwen delegate 每个调用只执行一次。 |
| 16 | `test_agent_loop_defers_streamed_barrier_tool_calls_until_stream_finishes` | `test_current_runtime_tool_adapter_serializes_unsafe_jiuwen_tool_invocations`、`test_registry_uses_barriered_parallel_execution` | barrier 语义下沉到 Jiuwen adapter 与本地 executor。 |
| 17 | `test_stream_closed_tool_calls_execute_with_triggering_assistant_context` | `test_current_runtime_tool_adapter_preserves_pending_assistant_context` | 触发工具的 assistant content/reasoning 由 adapter 绑定。 |
| 18 | `test_stream_closed_tool_calls_do_not_deepcopy_entire_state` | `test_apply_tool_batch_result_to_state_merges_runtime_fields` | native worker 的 deepcopy 优化路径退役；共享状态合并只更新明确字段。 |
| 19 | `test_iteration_metrics_exclude_streamed_tool_overlap_from_llm_elapsed` | `test_build_tool_timing_metrics_computes_overlap_and_parallel_execution`、`test_jiuwen_loop_reports_parallel_tool_window_in_iteration_metrics` | 统一计时 helper 与 Jiuwen 并行执行集成覆盖 overlap。 |
| 20 | `test_iteration_metrics_report_tool_net_wait_and_wall_time` | `test_build_tool_timing_metrics_computes_overlap_and_parallel_execution` | net wait、wall time 改由共享纯函数覆盖。 |
| 21 | `test_streamed_tool_net_wait_uses_llm_stream_end_before_wait_all` | `test_build_tool_timing_metrics_computes_overlap_and_parallel_execution`、`test_jiuwen_loop_reports_parallel_tool_window_in_iteration_metrics` | native `wait_all` 时序退役；Jiuwen tool window 使用 LLM 完成边界。 |
| 22 | `test_tool_timing_metrics_report_zero_net_wait_when_tools_are_fully_covered_by_llm` | `test_build_tool_timing_metrics_computes_overlap_and_parallel_execution` | 零 net-wait 属于共享 timing helper 的边界输入。 |
| 23 | `test_tool_timing_metrics_treat_tools_after_llm_as_net_wait` | `test_build_tool_timing_metrics_computes_overlap_and_parallel_execution` | LLM 后工具耗时属于共享 timing helper 的边界输入。 |
| 24 | `test_agent_loop_merges_streamed_state_updates_across_closed_batches` | `test_apply_tool_batch_result_to_state_merges_runtime_fields`、`test_jiuwen_loop_keeps_multiple_tools_from_one_llm_turn_in_same_iteration` | 状态合并与同轮多工具分别覆盖。 |
| 25 | `test_agent_loop_filters_streamed_tool_calls_by_index_when_final_id_arrives_late` | `test_current_runtime_tool_adapter_uses_unique_tool_call_ids_per_invocation`、`test_jiuwen_loop_keeps_multiple_tools_from_one_llm_turn_in_same_iteration` | native index/final-id 聚合器退役；Jiuwen 使用 run-scoped call/batch ID。 |
| 26 | `test_agent_loop_executes_tool_calls_in_single_batch` | `test_jiuwen_loop_keeps_multiple_tools_from_one_llm_turn_in_same_iteration`、`test_execute_tool_batch_accepts_internal_tool_invocation` | 同一 LLM turn 的多工具保留同一 iteration，并统一转换为中立调用模型。 |
| 27 | `test_agent_loop_persists_selected_ontology_context_on_first_user_message` | `test_jiuwen_loop_engine_injects_selected_ontology_context_into_inputs` | 原行为保留。 |
| 28 | `test_agent_loop_builds_selected_ontology_context_once_per_run` | `test_jiuwen_loop_engine_injects_selected_ontology_context_into_inputs` | context 在 Jiuwen run 输入准备阶段一次性构建。 |
| 29 | `test_agent_loop_keeps_selected_ontology_context_on_first_user_message` | `test_jiuwen_loop_engine_appends_current_date_after_selected_ontology_context`、`test_jiuwen_loop_engine_seeds_persisted_history_into_new_context` | 首轮 ontology 输入顺序和持久历史均覆盖。 |
| 30 | `test_agent_loop_registers_selected_ontology_preload_records` | `test_jiuwen_loop_engine_registers_selected_ontology_preload_records` | 原行为保留。 |
| 31 | `test_agent_loop_terminal_user_interaction_tool` | `test_terminal_interaction_skips_later_tool_calls`、`test_current_runtime_tool_adapter_returns_terminal_content_without_callback` | adapter 正常返回 terminal 工具内容并保留状态，engine 的 AFTER_TOOL_CALL callback 请求 force finish；executor 继续覆盖后续工具短路。 |
| 32 | `test_agent_loop_returns_when_tool_batch_is_terminal` | `test_jiuwen_loop_returns_when_tool_batch_is_terminal`、`test_execute_tool_batch_stops_after_terminal_outcome` | Jiuwen force-finish 与本地 batch 短路均覆盖。 |
| 33 | `test_agent_loop_raises_when_iteration_limit_is_exceeded` | `test_jiuwen_loop_engine_registers_current_tools_for_default_agent`、`test_jiuwen_loop_engine_raises_when_agent_returns_error_result_type` | 最大迭代数传给 ReActAgent；旧 native for-loop exhaustion 异常分支退役。 |
| 34 | `test_agent_loop_binds_subagent_log_context_for_llm_and_tools` | `test_jiuwen_loop_binds_subagent_log_context_for_agent_and_tools`、`test_registry_propagates_log_context_to_parallel_tool_threads` | Agent、tool delegate 与并行 worker 的日志上下文均覆盖。 |

新的 facade 节点为：

- `test_agent_loop_engine_is_jiuwen_only_facade`
- `test_agent_loop_engine_delegates_mode_resolution`
- `test_agent_loop_engine_propagates_jiuwen_backend_unavailable`

### Phase 3 smoke 与压力证据

- `test_jiuwen_smoke_cleanup.py` 覆盖默认真实 Runner resource 注册后释放、注入式注册/清理和无副作用 validator。
- `scripts/jiuwen_runtime_pressure.py` 使用真实 OpenJiuwen 0.1.14 与离线模型完成 20 路并发、500/500 个短 run；owned resource、callback、metric、checkpoint 全部清零，Jiuwen loop 无遗留任务。完整结果见 `jiuwen-runtime-pressure.md`。

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
  backend/tests/test_agent_loop.py \
  backend/tests/test_jiuwen_react_adapter.py \
  backend/tests/test_loop_shared.py \
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
