# Jiuwen Runtime Warm-up 详细设计

## 1. 背景

当前本体消费智能体在服务重启后的第一次问答中，会把 Jiuwen 运行时冷启动成本叠加到用户请求路径上。一次实际日志显示：

- Agent run 在 `16:04:30` 创建并开始执行。
- 第一笔真实 LLM 请求到 `16:04:45` 左右才发出。
- 中间约 15 秒主要用于 openJiuwen 包导入、ReActAgent 相关类加载、LLM model client 配置对象构造、parser/connector/callback/tool registry 初始化、Jiuwen resource manager 初始化、后台 event loop 启动、工具 schema 转换等。

因此 warm-up 的目标不是优化整次复杂问答的全部耗时，而是把“首次请求前的 Jiuwen runtime 和 agent factory 冷初始化”前移到服务启动阶段，让用户第一次问答不再承担这段等待。

## 2. 设计目标

1. 将首问前的 Jiuwen 冷启动成本前移到服务启动后后台执行。
2. warm-up 默认不调用 LLM、不访问 MindsDB、不消耗 token。
3. warm-up 失败不影响服务启动，真实用户请求仍可按原路径冷启动执行。
4. warm-up 不保存或复用 agent 实例，避免污染真实请求状态。
5. 通过日志和内部状态提供可观测性，避免第一版扩大 HTTP API 面；失败时只记录失败结果，不记录异常细节。
6. warm-up 在单进程内只执行一次，成功或失败都视为本轮启动期预热结束，不做自动重试。
7. 改动保持小而可控，优先落在 app 启动入口和 Jiuwen 适配层。

## 3. 非目标

warm-up 不解决以下问题：

- 多轮 ReAct 推理导致的总耗时。
- 多次 `business_data_search` / MindsDB 查询等待。
- 复杂成本计算类问题的执行路径过长。
- LLM 首 token 延迟。
- 数据源连接池、MindsDB provider 复用、SQL 聚合等深层性能优化。

这些属于第二阶段优化，建议通过 direct execution、查询聚合或业务脚本化执行来处理。

## 4. 核心原则

### 4.1 Warm Factory，不 Warm Instance

warm-up 创建的 agent 不是用户请求使用的 agent。

```text
服务启动
  -> 创建 warm-up agent
  -> 触发 Jiuwen 构造链路和全局初始化
  -> warm-up 函数返回
  -> warm-up agent 离开作用域，被 GC 回收

用户请求
  -> 创建真实 request agent
  -> 复用已加载模块、全局 registry、后台 event loop 等运行环境
```

原因：

- 用户请求 agent 持有 `session_id`、`run_id`、workspace、history、tool context、checkpointer 等运行态状态。
- ReActAgent 不应假设可重入、可 reset 或线程安全。
- openJiuwen 全局资源可以复用，但业务 agent 实例不应跨请求复用。

### 4.2 默认无外部请求

第一版 warm-up 只做运行时预热，不做端到端 smoke query。

默认不执行：

- `agent.invoke(...)`
- `agent.stream(...)`
- `engine.run(...)`
- `business_data_search`
- MindsDB 查询
- 真实 LLM completion

这样可以避免 token 消耗、数据源副作用和启动期网络波动。

### 4.3 不注册假工具实例

`register_current_runtime_tools(...)` 需要真实的 `state` 和 `context_factory`。warm-up 阶段如果传入 dummy state，可能把假 delegate 注册进 Jiuwen 全局 resource manager，污染后续真实请求。

因此第一版只预加载工具模块和工具 schema，不注册带假上下文的工具实例。

## 5. 总体架构

```text
backend/run.py 或 wsgi.py
  -> agentic.create_app()
      -> build_runtime_services()
      -> configure_llm_router()
      -> register_routes()
      -> start_runtime_warmup(agent_config)
           -> 后台 daemon thread
              -> warm_up_jiuwen_runtime(agent_config)
                   -> require_openjiuwen()
                   -> 启动 Jiuwen long-lived event loop
                   -> 解析 MAIN LLM 配置
                   -> 构造 warm-up ReActAgent
                   -> 预加载工具 schema 和 Jiuwen tool/runner 模块
                   -> 丢弃 warm-up agent
      -> /health
```

## 6. 代码落点

### 6.1 `backend/agentic/loop/jiuwen_adapter.py`

新增函数：

```python
def warm_up_jiuwen_runtime(*, agent_config: Any = None) -> dict[str, Any]:
    ...
```

职责：

- 检查 openJiuwen 是否可用。
- 启动 `_JIUWEN_LOOP_RUNNER` 持有的后台 event loop。
- 解析主 Agent 的 LLM 配置。
- 构造一个 warm-up 专用 `ReActAgent`。
- 预加载 Jiuwen tool/runner 模块和当前 runtime 工具 schema。
- 返回预热元信息。
- 不保存 agent 引用。
- 不调用模型和工具。

建议伪代码：

```python
def warm_up_jiuwen_runtime(*, agent_config: Any = None) -> dict[str, Any]:
    started = time.perf_counter()

    require_openjiuwen()
    _JIUWEN_LOOP_RUNNER.get_loop()

    llm_config = _resolve_jiuwen_llm_config(
        {},
        caller="main",
        agent_config=agent_config,
    )

    agent = create_default_jiuwen_agent(
        llm_config=llm_config,
        max_iterations=1,
        system_prompt="warmup",
        agent_id="onto_agent_jiuwen_react.warmup",
        enable_reasoning=False,
        stream_enabled=True,
    )

    get_jiuwen_tool_infos("main")
    importlib.import_module("openjiuwen.core.foundation.tool")
    importlib.import_module("openjiuwen.core.runner")

    del agent

    return {
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "mode": "jiuwen-runtime",
    }
```

注意：如果 `_resolve_jiuwen_llm_config(...)` 会读取当前系统配置，日志中不得输出 `api_key`、`.env` 值或凭据。

### 6.2 `backend/agentic/warmup.py`

新增 warm-up 一次性启动模块。该模块不是复杂生命周期管理器，只负责启动期触发一次、保证幂等、记录最终状态和日志。

建议数据结构：

```python
@dataclass
class RuntimeWarmupStatus:
    enabled: bool
    state: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    mode: str | None = None
    error_type: str | None = None
    error_message: str | None = None
```

`error_type` 和 `error_message` 保留为兼容字段，但第一版失败时不填充，避免异常对象、底层配置或第三方库错误消息间接暴露凭据。

状态枚举：

```text
disabled
idle
running
success
failed
```

建议导出函数：

```python
def start_runtime_warmup(*, agent_config: Any = None) -> RuntimeWarmupStatus:
    ...

def get_runtime_warmup_status() -> RuntimeWarmupStatus:
    ...
```

职责：

- 读取 warm-up 开关。
- 保证同一进程内只启动一次 warm-up。
- 使用后台 daemon thread 执行，避免阻塞 Flask 启动。
- 捕获异常并更新状态，不向外抛出影响服务启动。
- 成功和失败都作为终态，不自动重试。
- 日志只记录生命周期状态和成功耗时；失败只记录 `state=failed`，不记录异常类型、异常消息或 traceback。
- 状态快照在锁内复制返回，锁内不再调用会二次加锁的状态读取函数，避免默认关闭 warm-up 时死锁。

线程模型：

```text
main thread
  -> start_runtime_warmup()
      -> if disabled: state=disabled
      -> if already running/success/failed: return current status
      -> spawn daemon thread

warmup thread
  -> state=running
  -> warm_up_jiuwen_runtime(...)
  -> state=success 或 failed
  -> 本进程不再自动重试
```

### 6.3 `backend/agentic/app.py`

在 `create_app()` 内挂载：

```python
from .warmup import start_runtime_warmup
```

调用点建议放在 `register_routes(...)` 之后：

```python
register_routes(
    app,
    runtime["user_store_manager"],
    agent_config=runtime["agent_config"],
)

start_runtime_warmup(agent_config=runtime["agent_config"])
```

第一版只在 `create_app()` 启动阶段触发 warm-up，不在 `refresh_runtime_services(...)` 配置刷新路径中再次触发。配置中心或管理界面变更 `runtime_warmup_enabled` 后，在下一次服务进程启动时生效。

第一版不新增 readiness 接口。`/health` 保持当前轻量存活检查语义，不被 warm-up 状态影响。

原因：

- warm-up 是性能优化，不是服务可用性的必要条件。
- warm-up 失败后真实请求仍可按原路径冷启动执行。
- 新增 `/health/ready` 会扩大 API 面，并容易让部署编排误以为 warm-up 失败等同于服务不可用。
- 当前最小实现只需要日志和内部状态，必要时后续再增加诊断接口。

## 7. 配置设计

第一版进入现有系统配置，同时保留项目当前配置层的 env fallback 风格。

当前系统配置的来源在 `backend/agentic/config/system_config.py`，配置读取入口在 `backend/agentic/config/settings.py`。warm-up 属于 Agent 运行行为，应放在 `agent` section。

建议新增字段：

```text
agent.runtime_warmup_enabled: bool
```

### 7.1 `agent.runtime_warmup_enabled`

含义：

- `true`：服务启动后后台预热 Jiuwen runtime。
- `false`：不做 warm-up，维持当前行为。

建议：

- 默认值为 `true`，让测试和常规部署默认覆盖启动期 warm-up 路径，尽早暴露兼容性问题。
- 需要保持旧行为、压测冷启动或极简本地启动的环境，可以在系统配置中显式设置为 `false`。
- 配置中心或管理界面保存后，可以通过现有 `refresh_runtime_services(...)` 路径刷新 `AgentConfig`，但不会在当前进程重新触发 warm-up。
- warm-up 开关变更后，在下一次服务进程启动时生效。

`system_config.py` 建议增加：

```python
"runtime_warmup_enabled": {
    "env": "AGENT_RUNTIME_WARMUP_ENABLED",
    "type": "bool",
    "default": True,
},
```

`settings.py` 建议在 `AgentConfig` 增加默认字段，避免影响测试中直接构造 `AgentConfig(...)` 的代码：

```python
runtime_warmup_enabled: bool = True
```

并在 `AgentConfig.from_env()` 中读取：

```python
runtime_warmup_enabled=bool(config.get("runtime_warmup_enabled", True))
```

### 7.2 环境变量 fallback

第一版不设计单独的 `AGENTIC_WARMUP_ENABLED` 主开关，而是沿用系统配置字段的 env fallback：`AGENT_RUNTIME_WARMUP_ENABLED`。

原因：

- 项目现有 `agent` 配置字段普遍支持 env fallback。
- warm-up 的主语义仍属于系统配置 `agent.runtime_warmup_enabled`。
- env fallback 只作为现有配置层的兼容入口，不在 warm-up 模块内单独读取环境变量。

## 8. 状态观测语义

第一版只保留进程内状态和日志，不新增 HTTP readiness 接口。

内部状态建议：

| state | 含义 |
| --- | --- |
| disabled | 系统配置关闭 warm-up |
| idle | 尚未启动 |
| running | 后台预热中 |
| success | 预热成功，本进程不再自动重试 |
| failed | 预热失败，真实请求仍可冷启动，本进程不再自动重试 |

日志需要覆盖：

- warm-up 是否被系统配置关闭。
- warm-up 开始时间。
- warm-up 成功耗时。
- warm-up 失败结果。

日志不记录失败异常类型、异常消息或 traceback。失败原因仍由真实用户请求路径在实际触发时暴露原有错误，不由启动期 warm-up 额外输出。

如后续确实需要前端展示或运维探测，可再增加诊断接口，但不纳入第一版最小实现。

## 9. Flask Reloader 处理

`backend/run.py` 当前默认 `SERVER_AUTO_RELOAD=False`，通常不会重复 warm-up。

但为兼容开发模式，如果未来打开 Flask reloader，`create_app()` 可能在父进程和子进程各执行一次。warm-up 管理模块应避免父进程重复启动。

第一版建议保持保守策略：

- 不在 `create_app()` 中依赖 `FLASK_DEBUG` 或 `WERKZEUG_RUN_MAIN` 推断最终 reloader 行为。
- `start_runtime_warmup(...)` 只做进程内 one-shot 幂等：同一进程内 `running/success/failed` 都不重复启动。
- 如果未来确实需要精确区分 Werkzeug reloader 父子进程，应优先在启动入口处理，而不是在 app factory 中猜测。
- 当前默认 `SERVER_AUTO_RELOAD=False`，因此第一版不需要额外 reloader 分支即可满足主场景。

## 10. 异常处理

warm-up 异常分两类：

1. openJiuwen 不可用或 import 失败。
2. 配置解析、agent 构造或 event loop 启动失败。

处理方式：

- 捕获异常。
- 状态置为 `failed`。
- 记录 warning 日志，但只记录失败结果。
- 不抛出到 `create_app()`。
- 不阻断 `/health`。
- 真实请求仍按原逻辑执行，并在需要时暴露原错误。
- 不在 warm-up 状态或日志中保存异常类型、异常消息或 traceback。

日志示例：

```text
Runtime warm-up started mode=jiuwen-runtime
Runtime warm-up finished state=success duration_ms=...
Runtime warm-up finished state=failed
```

日志中禁止输出：

- API key
- token
- 私钥
- `.env` 原始值
- 数据源密码
- 失败异常消息或 traceback

## 11. 预期收益

warm-up 节约的是首问进入真实 LLM 请求前的 Jiuwen runtime 冷初始化时间，主要包括：

1. **Python 模块导入时间**：`openjiuwen` 及其 core、single_agent、foundation、tool、runner、callback 相关模块的首次 import。
2. **Jiuwen ReActAgent 构造链路时间**：加载 `AgentCard`、`ReActAgent`、`ReActAgentConfig`、`ModelClientConfig`、`ModelRequestConfig` 等类，并完成 warm-up agent 的配置。
3. **LLM client 配置对象初始化时间**：根据主 Agent 的 LLM 配置构造 Jiuwen model client/request config。该过程只构造配置对象，不发起真实 completion。
4. **全局 runtime 资源初始化时间**：Jiuwen callback framework、resource manager、runner 等全局或半全局对象的首次加载和初始化。
5. **后台 event loop 启动时间**：启动 `_JIUWEN_LOOP_RUNNER` 持有的 long-lived asyncio loop，避免真实请求首次进入时再创建和绑定 loop。
6. **工具 schema 预加载时间**：将当前 runtime 工具规范转换为 Jiuwen 可识别的 tool info/schema，预加载 Jiuwen tool 相关模块。

warm-up 不节约以下耗时：

- 真实 LLM completion 的首 token 延迟。
- ReAct 多轮推理时间。
- `business_data_search` 工具执行时间。
- MindsDB 查询或数据源连接时间。
- 复杂业务计算、SQL 聚合、脚本执行时间。

基于一次实际日志估算：

```text
首问冷启动段：约 15s
warm-up 可前移节约：约 8-12s，理想情况下 10-15s
```

复杂问题整体耗时仍主要由以下部分决定：

- 多轮 ReAct 推理。
- 多次业务数据查询。
- MindsDB 响应延迟。
- 多轮 LLM 流式输出。

因此，对复杂成本计算问题，warm-up 后预期效果是：

```text
当前首问总耗时：约 180s
只做 Jiuwen warm-up 后：约 165-170s
```

对简单问答，warm-up 的体感收益会更明显，因为冷启动段占比更高。

## 12. 测试方案

### 12.1 单元测试

新增 `backend/tests/test_runtime_warmup.py`。

覆盖点：

1. `agent.runtime_warmup_enabled=false` 时状态为 `disabled`，且不会因锁内重复取锁导致应用启动卡死。
2. `agent.runtime_warmup_enabled=true` 时只启动一次后台线程。
3. 未显式配置时默认启用 warm-up。
4. warm-up 成功后状态为 `success`，记录 duration，后续再次调用不重启。
5. warm-up 抛异常后状态为 `failed`，服务不抛异常，后续再次调用不重试。
6. warm-up 失败时状态对象不保存异常类型或异常消息。
7. warm-up 失败日志只包含 `state=failed`，不包含异常消息、密钥字段或疑似凭据值。
8. `AGENT_RUNTIME_WARMUP_ENABLED=false` 作为 env fallback 时能关闭 warm-up。

### 12.2 系统配置测试

在 `backend/tests/test_system_config_settings.py` 和 `backend/tests/test_system_config_api.py` 补充：

1. `get_system_config_template()` 暴露 `agent.runtime_warmup_enabled=true` 默认值。
2. `update_system_config_section("agent", {"runtime_warmup_enabled": False})` 能保存并返回公共配置。
3. `AgentConfig.from_env()` 默认读取 `runtime_warmup_enabled=True`。
4. `AGENT_RUNTIME_WARMUP_ENABLED=false` 作为系统配置字段 env fallback 时能关闭 warm-up。
5. 导入配置时，新字段能进入 `agent` section，未知字段仍按现有规则过滤。

### 12.3 Jiuwen adapter 测试

在 `backend/tests/test_jiuwen_react_adapter.py` 补充：

1. `warm_up_jiuwen_runtime()` 会调用 `require_openjiuwen()`。
2. 会构造 warm-up agent。
3. 不调用 agent `invoke` / `stream`。
4. 不调用 `register_current_runtime_tools(...)` 绑定假 state。

### 12.4 建议命令

```powershell
python -m pytest backend/tests/test_runtime_warmup.py backend/tests/test_jiuwen_react_adapter.py backend/tests/test_system_config_settings.py backend/tests/test_system_config_api.py -q
```

如果本地 Windows 环境存在“系统 Python 有 pytest、backend venv 有 Flask 但没有 pytest”的拆分情况，可临时把 `backend` 和 `backend/venv/Lib/site-packages` 加入 `sys.path` 后运行同一组测试。

原因：

- 覆盖新增 warm-up 一次性启动逻辑。
- 覆盖系统配置模板、保存、导入和 `AgentConfig` 读取链路。
- 覆盖 Jiuwen adapter 的既有边界，避免破坏真实请求路径。

## 13. 手工验证方案

默认配置下服务启动后会自动触发 warm-up。如需临时关闭，可在系统配置中设置，或通过 `AGENT_RUNTIME_WARMUP_ENABLED=false` 使用 env fallback：

```json
{
  "agent": {
    "runtime_warmup_enabled": false
  }
}
```

启动服务：

```powershell
python backend/run.py
```

验证点：

1. 服务启动后日志出现 warm-up started。
2. warm-up 完成后日志显示 `state=success` 和耗时。
3. 如果 warm-up 失败，日志只显示 `state=failed`，不输出异常类型、异常消息或 traceback。
4. 第一次真实问答仍创建新的 request agent。
5. 首问从 run 创建到第一笔 LLM 请求的间隔明显缩短。
6. 没有额外 LLM token 消耗记录。
7. 没有提前触发 MindsDB 或业务数据查询。

## 14. 分阶段实施建议

### 阶段 1：Runtime Warm-up

实施内容：

- 新增 `warm_up_jiuwen_runtime(...)`。
- 新增 warm-up 一次性启动模块。
- 接入 `create_app()`。
- 增加单元测试。

收益：

- 解决首问 Jiuwen 初始化等待。
- 风险低，无外部请求。

### 阶段 2：Provider/Client 预热评估

可选内容：

- 评估 MindsDB provider 是否适合连接池化。
- 评估 `business_data_search` 是否可以复用 provider/client。
- 只在确认无副作用后引入。

风险：

- 可能引入网络请求。
- 可能受数据源状态影响。
- 可能改变连接生命周期。

### 阶段 3：复杂问题执行路径优化

针对成本汇总类问题，warm-up 不是主要瓶颈。后续应考虑：

- direct execution router。
- 本体 operation 对应脚本化执行。
- 多查询聚合。
- 减少 ReAct 轮数。
- 将确定性成本计算从 LLM 多轮推理迁移到受控执行逻辑。

## 15. 推荐最小实现清单

第一版只做以下最小改动：

1. `backend/agentic/loop/jiuwen_adapter.py`
   - 增加 `warm_up_jiuwen_runtime(...)`。
2. `backend/agentic/warmup.py`
   - 增加一次性状态管理、后台线程、系统配置开关读取。
3. `backend/agentic/config/system_config.py`
   - 在 `agent` section 增加 `runtime_warmup_enabled` 字段和 env fallback。
4. `backend/agentic/config/settings.py`
   - 在 `AgentConfig` 增加 `runtime_warmup_enabled: bool = True` 并从系统配置读取。
5. `backend/agentic/app.py`
   - 调用 `start_runtime_warmup(...)`。
6. `backend/tests/test_runtime_warmup.py`
   - 覆盖 warm-up 一次性启动逻辑。
7. `backend/tests/test_system_config_settings.py`
   - 覆盖系统配置字段、env fallback 和 `AgentConfig.from_env()`。
8. `backend/tests/test_system_config_api.py`
   - 覆盖配置模板和 API 保存返回。
9. `backend/tests/test_jiuwen_react_adapter.py`
   - 覆盖 Jiuwen adapter warm-up 边界。

这个版本能解决重启后第一次问答前的 Jiuwen 冷启动等待，同时不改变真实请求 agent 生命周期。
