# 可观测性运行时说明

本文说明 `backend/integration/observability` 的职责边界。核心原则是：业务执行代码只表达“发生了什么”，不直接理解本地文件、Langfuse、trace id 或 SDK 生命周期。

## 事件流

```text
RunRuntimeTrace / AgentRuntimeTrace / LLMRuntimeTrace
        |
        v
RuntimeEvent
        |
        v
get_observer() 返回的 RuntimeObserver.emit(...)
        |
        v
RuntimeEventDispatcher
        |
        +--> WorkspaceObservabilityRouter
        |       |
        |       +--> ObservabilityBus(_WorkspaceExporterFanout)
        |               |
        |               +--> mode=local -> LocalPerfExporter -> workspace 本地 OpenTelemetry JSONL record
        |               +--> mode=langfuse -> LangfuseExporter -> LangfuseTraceAdapter -> Langfuse SDK client
        |               +--> 两种 mode -> SpeedscopeExporter -> 会话级 .speedscope.json
        |
        +--> Platform manager observer
                |
                +--> ObservabilityBus(PlatformExporter) -> onto-platform connector
```

全局 system config 不再提供 `observability` section。本地 perf 和 Langfuse 观测配置按 workspace 保存：

```text
.onto-runtime/workspaces/<workspace_id>/observability_config.json
```

每个 workspace 继续选择一个观测数据输出目标：

- `mode=local`：异步写该 workspace 下的本地 OpenTelemetry JSONL。
- `mode=langfuse`：使用该 workspace 的 Langfuse 地址和密钥发送到对应 Langfuse project，不同时写本地记录。
- 两种模式都生成 Speedscope 会话文件。
- `enabled=false`：该 workspace 不输出 local/Langfuse 观测数据。

`enabled=false` 只影响 workspace 观测，不影响 onto-platform connector。PlatformExporter 的配置和生命周期由 `backend/agentic/platform/manager.py` 独立管理，继续读取 `.onto-runtime/config/platform_config.json`。

本地文件模式通过 workspace 配置中的 `local.retention_days` 按日期分区老化日志，默认保留最近 30 天。新 perf 文件只写入：

```text
.onto-runtime/workspaces/<workspace_id>/runtime/observability/otel_trace-YYYY-MM-DD.log
```

旧 `logs/agent_perf-*` 不迁移、不读取。

## 本地性能分析

`mode=local` 写出的本地 OpenTelemetry JSONL 可在系统内分析：

- 聊天区“性能分析”只读取当前会话关联到的本地 perf 记录。
- 管理员用户菜单“本地观测分析”读取 workspace perf 目录内的保留窗口记录，可按工作空间、状态、模型供应商和 Agent 模式过滤；不指定 workspace 时聚合所有 workspace。
- 会话页和管理员页共用 run 下钻，展示耗时拆分、iteration、LLM、工具、runtime phase 和事件时间线。

分析读路径位于 `backend/utils/perf_analysis/`，与 exporter 写路径分离。它只接受新的 `schema_version=2` OpenTelemetry envelope，按时间窗口扫描 workspace 日期分片，不读取 Langfuse 数据，也不兼容旧 direct perf logger 或旧 `logs/agent_perf-*` 历史记录。

## 分层职责

### Trace 门面层

文件：

- `traces/run.py`
- `traces/agent.py`
- `traces/llm.py`

职责：

- 提供面向业务执行代码的 API，例如 `run_trace.started(...)`、`runtime_trace.llm_call_scope(...)` 和 `runtime_trace.tool_batch_scope(...)`。
- 将运行时概念转换成与具体观测厂商无关的 `RuntimeEvent`。
- 统一成功、失败、取消等生命周期语义。

不负责：

- 调用 Langfuse SDK。
- 网络 I/O。
- 队列或重试策略。
- 管理具体观测厂商的 trace/span id。

### RuntimeEvent

文件：`events.py`

职责：

- 定义运行时代码和 exporter 之间共享的内部事件契约。
- 承载稳定的 run/session/turn/span 身份信息以及事件载荷。

不负责：

- 决定观测厂商如何建模 traces、spans、generations 或 usage。
- 导出或缓存事件。

### RuntimeObserver / NoopObserver

文件：`observer.py`

职责：

- 给运行时代码提供稳定的 `emit(event)` 接口。
- 在关闭可观测性时提供空操作路径，避免主执行链路散落条件分支。

不负责：

- 初始化具体观测厂商。
- 提供事件投递保证。

### RuntimeEventDispatcher

文件：`dispatcher.py`

职责：

- 将同一个 `RuntimeEvent` 扇出到独立 sink，例如 workspace 观测和 platform connector。
- 隔离单个 sink 的 emit/close 异常，避免影响其他 sink 和 Agent 主链路。
- 通过 `close_on_close` 区分生命周期所有权，workspace observer 可随 `reset_observer()` 关闭，platform observer 由 platform manager 自己关闭。

不负责：

- 读取 workspace 或 platform 配置。
- 构造 exporter。
- 改写事件载荷。

### 观测提供器

文件：`provider.py`

职责：

- 维护进程级共享 observer，避免每次 agent run 都创建新的 bus、exporter、SDK client 和后台 worker。
- 通过 `get_observer()` 为运行时代码提供当前 observer。
- 在 workspace 观测配置刷新时通过 `reset_observer()` 丢弃旧 workspace observer，让后续运行按新配置重新初始化。
- 在进程退出或测试清理时通过 `shutdown_observer()` 关闭当前 observer。

不负责：

- 管理 platform connector 的生命周期。
- 生成 run/session/turn/span 身份信息。
- 构造 `RuntimeEvent`。
- 决定 trace、span 或 generation 的业务语义。
- 在单次 agent run 结束时关闭 observer。

### WorkspaceObservabilityRouter

文件：`workspace_router.py`

职责：

- 根据 `run.started` 的 `workspace_id` 建立 `run_id -> workspace_id` 绑定。
- 按 workspace 延迟创建 local 或 Langfuse exporter，并在后续 LLM/tool/runtime 事件中复用同一 exporter。
- 在 run 终态事件后清理绑定。
- 将某个 workspace 的配置错误或 exporter 失败限制在该 workspace 内。

不负责：

- platform connector 上报。
- 保存 workspace 观测配置。
- 分析本地 perf 文件。

### ObservabilityBus

文件：`bus.py`

职责：

- 通过 `queue.put_nowait(...)` 接收事件，避免 agent 执行等待 exporter I/O。
- 在队列满时丢弃事件。
- 在后台 worker 中 drain、flush 并关闭 exporter。
- 将 exporter 失败隔离在 agent 运行之外。

不负责：

- 理解事件业务语义。
- 构造具体观测厂商的 trace 树。
- 在进程退出后重试或保证投递。

### RuntimeExporter

文件：`exporters/base.py`

职责：

- 定义 `ObservabilityBus` 面向观测厂商的边界。
- 保持 bus 与本地文件、Langfuse 或未来其他观测后端解耦。

不负责：

- 构造运行时事件。
- 管理队列。

### LocalPerfExporter

文件：`exporters/local/exporter.py`

职责：

- 将与后端无关的运行时事件投影为有界本地性能记录。
- 复用日志轮转基础设施写 JSONL。
- 在 terminal run 后补充轻量 `run_summary`。
- 默认只写耗时、计数、规模和有界 label，不保存完整 prompt、reasoning、completion 或 tool 输出。

不负责：

- 构造运行时事件。
- 阻塞 agent 主执行链路。
- 作为 Langfuse rich trace 的离线副本。

### LangfuseExporter

文件：`exporters/langfuse/exporter.py`

职责：

- 将与厂商无关的运行时事件映射为 Langfuse trace/span/generation 操作。
- 管理单次运行内 runtime `span_key` 和 Langfuse observation id 的对应关系。
- 决定 Langfuse 侧的事件名称、tags、metadata 结构和终态 status 映射。

不负责：

- 加载 Langfuse 配置。
- 绕过 adapter 直接调用 Langfuse SDK。
- 在 adapter 之外清洗任意嵌套载荷。
- 阻塞 agent 主执行链路。

### LangfuseTraceAdapter

文件：`exporters/langfuse/client.py`

职责：

- 延迟创建并封装 Langfuse SDK client。
- 执行 SDK 操作：create trace、create span/generation、end observation、update output、flush、close。
- 在数据进入 SDK 前执行当前尽力而为的数据清洗。
- 在终态 output 写入且所有 observations 结束后释放 active trace 状态。

不负责：

- 理解 agent loop 的业务语义。
- 决定系统中有哪些运行时事件。
- 决定一次 agent run 何时开始、完成、失败或取消。

## 为什么需要这些分层

这些分层是有意保留的：

- 运行时代码保持与观测厂商无关。
- exporter I/O 与 Agent 主执行链路隔离。
- observer 基础设施按进程复用，单次 agent run 只持有引用，不管理其生命周期。
- 关闭可观测性时走空操作路径，而不是在主链路散落条件判断。
- 观测厂商相关逻辑可以独立演进，不需要改写 `orchestrator.py` 或 `loop/engine.py`。

建议的阅读顺序：

- 先从 `traces/` 中的门面方法开始。
- 再跟随它 emit 出来的 `RuntimeEvent`。
- 只有需要理解厂商映射时，再看 `LangfuseExporter`。
- 只有需要理解 SDK 调用、数据清洗或 flush/close 行为时，再看 `LangfuseTraceAdapter`。
## Speedscope session traces

Workspace observability is the only Speedscope control plane. `enabled=false` writes no local OpenTelemetry, Langfuse, or Speedscope data. With `enabled=true`, both `mode=local` and `mode=langfuse` generate the workspace session's evented `.speedscope.json` trace from unified spans and events. The trace is stored below `runtime/observability/speedscope/<user>/` and uses `speedscope.retention_days` for retention. The session download endpoint is `GET /agentic/sessions/<session_id>/performance/speedscope`; regular users are limited to their own sessions and workspace admins may download any session in the workspace.
