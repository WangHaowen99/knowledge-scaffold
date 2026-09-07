# OpenJiuwen ContextEngine 最终切换方案

> 更新时间：2026-09-01
> 本方案以“ContextEngine 优先、保持现有 ask-user 机制、不采用影子模式”为最终边界。
> 已吸收 `openjiuwen-context-engine-final-cutover-plan-review.md` 中的代码核对意见。

## 1. 最终决策

本次只完成一件事：

**将 Jiuwen ReAct 调用前的模型上下文窗口管理，从项目自研的 `apply_sliding_window` 切换为 OpenJiuwen ContextEngine。**

本阶段明确不做：

- 不切换 `request_user_interaction`；
- 不引入 AskUserRail；
- 不引入 Checkpointer；
- 不改变 ExecutionStore 的会话、运行和恢复模型；
- 不启用 ContextEngine 的压缩、卸载或自定义 Processor；
- 不采用影子窗口或双窗口并行比对；
- 不修改前端、SDK 和用户交互协议。

因此，本次切换属于“模型窗口管理组件替换”，不是整个会话状态系统的重构。

## 2. 切换后的职责边界

| 能力 | 切换后负责人 | 本次是否变更 |
|---|---|---|
| 完整原始消息历史 | ExecutionStore | 否 |
| run/turn 状态 | ExecutionStore / Orchestrator | 否 |
| `awaiting_input` 状态 | 现有问询流程 | 否 |
| `pending_user_interaction` | 现有 `request_user_interaction` | 否 |
| 用户回复校验和恢复 | 现有 Orchestrator / ExecutionStore | 否 |
| 当前 LLM 请求可见消息窗口 | ContextEngine | 是 |
| 工具调用与工具结果配对 | 项目历史转换 + OpenJiuwen ReAct 工具写入 | ContextEngine 负责轮次裁窗，并清理窗口开头的孤立 ToolMessage |
| 跨请求 ContextEngine 状态持久化 | 暂不启用 | 否 |

核心原则：

1. ExecutionStore 继续保存完整历史，是业务会话和恢复的事实来源。
2. ContextEngine 只负责为当前模型调用生成受控窗口。
3. 每次请求都从 ExecutionStore 的完整历史重建 Jiuwen 输入。
4. `request_user_interaction` 仍是 terminal tool，仍由现有机制中断并等待下一次请求。

## 3. 当前链路与目标链路

### 3.1 当前链路

```text
ExecutionStore 完整历史
    ↓
项目 apply_sliding_window
    ↓
转换为 Jiuwen 消息
    ↓
ContextEngine 再按 default_window_round_num 管理
    ↓
ReAct / LLM
```

当前问题是：项目窗口和 ContextEngine 窗口同时存在，形成双重裁剪。ContextEngine 实际收到的不是完整历史，因此无法独立承担上下文管理职责。

### 3.2 目标链路

```text
ExecutionStore 完整历史
    ↓
完整转换为 Jiuwen 消息
    ↓
ContextEngine 按 default_window_round_num 生成模型窗口
    ↓
ReAct / LLM / Tool
    ↓
完整结果继续写回 ExecutionStore
```

切换后只有 ContextEngine 决定“本次 LLM 能看到什么”，ExecutionStore 仍保存“整个业务会话发生过什么”。

## 4. 最小实现方案

### 4.1 修改 Jiuwen 历史与观测构建

修改：

`consumer/backend/agent/core/jiuwen_engine.py`

调整 `_build_jiuwen_history_messages`：

- 移除 Jiuwen 生产链路对项目 `apply_sliding_window` 的调用；
- 不再在转换前按 `context_max_rounds` 裁剪历史；
- 不再由项目层提前清理工具消息；
- 将完整历史转换为 Jiuwen 消息后交给 ContextEngine；
- 保持当前请求的 user message 去重逻辑，避免当前问题同时出现在 history 和 invocation input 中；
- 保持现有 system message 处理规则。

同时调整 `_build_jiuwen_context_messages`：

- 移除其中第二处 `apply_sliding_window`；
- 不再用项目滑窗结果估算 ContextEngine 的模型窗口；
- 该函数只构造一次“本次 agent run 启动时的候选消息快照”，包括完整历史、当前 query 和 system prompt；默认自建 agent 会将历史交给 ContextEngine，无历史 factory 路径仅将该快照用于旧观测字段；
- `iteration_prepared` 后续迭代复用同一份快照，因此 `llm_message_count`、`llm_input_chars` 既不是最终窗口，也不是每次 ReAct 迭代的候选窗口；
- 这两个字段只能用于判断本次 run 输入 ContextEngine 前的历史规模。

同步处理观测字段：

- 不再将两处 `sliding_window_applied=True` 作为项目滑窗已执行的硬编码事实；
- `sliding_window_applied` 保留为旧观测结构的兼容字段：True 仅表示项目为默认自建 agent 配置了由 Jiuwen ContextEngine 管理的轮次窗口，False 表示当前路径无法保证该配置；它不表示项目层已经修改或裁剪消息，也不作为“最终模型窗口已生成”的证据；
- 默认自建 agent 路径由项目明确注入 `ContextEngineConfig`，该标识为 True；
- 主 ReAct 的 `_agent_factory` 路径无法证明工厂内部启用了相同的轮次窗口，本阶段不扩展 factory 能力协议：有历史消息或 `interaction_response` 时明确拒绝，只有二者都不存在的单轮测试/注入调用允许继续并记录 False；
- graph 内部 factory 路径是独立的单轮查询链路，不纳入本次多轮上下文切换；其 trace 必须显式记录 False，不继续依赖 `_JiuwenModelTraceBridge` 的默认 True；
- `_JiuwenModelTraceBridge.__init__` 的 `sliding_window_applied` 改为必传参数，移除当前默认 True，所有调用点必须显式声明；
- 不新增观测字段，不修改现有导出结构，避免扩大兼容范围；
- 对应测试只验证该字段符合上述路径规则，不能将它作为窗口正确性的验收依据。

openJiuwen 0.1.17 的 `before_model_call` 消息是 **最终窗口生成前的 preview**。`ReActAgent` 会在该回调之后调用 `get_context_window()`，再用返回值覆盖 `ctx.inputs.messages` 并调用模型。因此：

- 不再把 `before_model_call` 解释为实际模型窗口；
- 默认 local exporter 也不持久化 `LLM_STARTED.input.messages`，开启 runtime trace 不能解决该问题；
- 本次发布验收使用真实 openJiuwen 0.1.17 和 fake model，在 fake model 的 `invoke()` / `stream()` 入参处直接断言最终 messages；
- 生产发布不记录消息正文，避免引入新的敏感上下文采集；
- 如果后续确实需要生产级实际窗口观测，必须单独增加“窗口生成后、模型调用前”的观测点，并同时设计 exporter 白名单、内容脱敏、访问控制和保留周期，不纳入本次最小切换。

由于 `llm_message_count`、`llm_input_chars` 字段名保持不变，但值是 run 起始候选快照，现有面板和告警阈值必须同步复核，避免继续按“最终窗口大小”或“每轮窗口大小”解释。

ContextEngine 配置继续使用当前已接入的配置：

```python
ContextEngineConfig(
    max_context_message_num=None,
    default_window_round_num=effective_rounds,
    enable_reload=False,
)
```

本次 0.1.17 兼容升级同时通过 `ReActAgentConfig` 显式固定以下配置：

```python
ReActAgentConfig(
    ...,
    context_engine_config=context_engine_config,
    kv_cache_affinity_config={
        "enable_kv_cache_release": False,
        "enable_kv_cache_affinity": False,
    },
    context_processors=[],
    parallel_tool_calls=True,
)
```

这些值用于保持当前行为：不启用 openJiuwen KV cache release/affinity 集成，不装载 ContextEngine 新处理器，并保持并行工具调用。openJiuwen KV cache release/affinity 集成、ContextEngine 新处理器等均属于后续独立能力适配，不在本次兼容升级中启用。

其中：

```python
configured_rounds = normalize(context_max_rounds)
effective_rounds = (
    max(configured_rounds, 2)
    if interaction_response
    else configured_rounds
)
```

`effective_rounds` 只用于本次默认 agent 的 ContextEngineConfig，不回写环境变量、系统配置或 ExecutionStore。这样在 `context_max_rounds=1` 的 ask-user 恢复请求中，ContextEngine 至少保留“上轮未完成工具问询 + 本轮用户回复”两个 incomplete round。

本阶段不引入尾部锚定 API、自定义 Processor 或动态兜底分支。

factory 拒绝和有效轮次计算必须发生在 `_create_agent` 之前，具体顺序固定为：

1. 从 `state["user_context"]["interaction_response"]` 读取并确认是否为恢复请求；
2. 以 `_drop_current_user_message` 相同口径，从 `state["messages"]` 中排除 system 和本轮刚补入的 current user，得到真实历史源消息；
3. 不能直接使用 `bool(state["messages"])`，因为 `_seed_initial_messages` 已经可能补入当前 system/user；
4. 当使用主 `_agent_factory` 且“真实历史非空或存在 interaction response”时，在调用 `_create_agent` 前显式拒绝；
5. 默认 agent 路径根据 interaction response 计算 `effective_rounds`，并将其传给 `create_default_jiuwen_agent`；
6. `_invoke_agent` 继续基于同一历史源消息做 Jiuwen 消息转换和 ContextEngine seed，避免拒绝判断与实际注入采用不同口径。

### 4.2 保留项目轮次窗口实现，删除工具名裁剪能力

项目现有 `apply_sliding_window` 暂不删除，但简化为只按最近 N 个 user round 裁窗，原因是：

- 当前生产代码虽未发现其他直接调用方，但该模块仍属于现有项目接口；
- 可作为紧急回退实现；
- 现有单元测试仍覆盖其轮次窗口行为；
- 删除属于后续清理，不应与本次行为切换绑定。

但它不再参与 Jiuwen + ContextEngine 的生产输入链路。

`context_prune_tools` 不再保留行为兼容，只保留升级读取兼容。它不是 ContextEngine 切换门禁，而是随本次切换直接删除的旧能力。

#### 4.2.1 代码删除范围

必须完成以下删除：

1. `consumer/backend/config/settings.py`
   - 删除 `AgentConfig.context_prune_tools` 字段；
   - 删除 `AgentConfig.from_env()` 中逗号分隔字符串转 `Set[str]` 的解析逻辑。
2. `consumer/backend/config/system_config.py`
   - 从 `_SECTION_FIELDS["agent"]` 删除 `context_prune_tools` 和 `AGENT_CONTEXT_PRUNE_TOOLS` 映射；
   - 从连接检查使用的临时 AgentConfig 构造中删除该字段。
3. `consumer/backend/agent/context.py`
   - 将接口固定为 `apply_sliding_window(messages, max_rounds)`；
   - 删除按工具名收集 `tool_call_id`、删除 ToolMessage、过滤 assistant `tool_calls` 的全部分支；
   - 保留的项目窗口实现只负责 system message 前置和最近 N 个 user round 裁窗。
4. `consumer/backend/agent/core/jiuwen_engine.py`
   - 删除两处 `agent_config.context_prune_tools` 读取；
   - 调用项目窗口时不再传入第三个参数。
5. `consumer/frontend/src/components/SystemSettings.jsx`
   - 删除 Agent 设置中的字段定义、分组入口和初始值；
   - 文案只描述上下文保留轮次，不再描述按工具名裁剪。
6. 配置示例和文档
   - 从 `.env.example`、API 文档、中文/英文 README 中删除该字段；
   - 压测脚本、测试 fixture 和所有 `AgentConfig(...)` 构造同步删除参数。

删除后不存在以下行为：

```text
按工具名匹配历史 ToolMessage
→ 收集 tool_call_id
→ 同时删除 ToolMessage 和 assistant tool_calls
```

保留轮次中的 assistant tool call 和 ToolMessage 必须原样进入后续历史转换或 ContextEngine。

#### 4.2.2 旧配置兼容和清理

兼容规则固定如下：

| 输入来源 | 新版本行为 |
|---|---|
| 旧 `system_config.json` 含该键 | 启动时忽略，不进入 effective/public config |
| 旧 `backend/.env` 含 `AGENT_CONTEXT_PRUNE_TOOLS` | 新 backend 不读取 |
| 新系统配置写接口提交该字段 | 按未知字段拒绝 |
| 前端读取旧 public config | 页面无对应字段，不展示也不回传 |

为了避免 code-only 回滚到旧 backend 后从当前 runtime/env 重新激活该能力，升级迁移还必须做一次幂等清理：

1. `migrate.sh --dry-run` 报告 `backend/.env` 和 `.onto-runtime/config/system_config.json` 中是否存在遗留项，但不因值非空阻断升级；
2. 旧栈停止且完成升级前备份后，`migrate.sh --confirm-writes-stopped`：
   - 删除 `backend/.env` 中的 `AGENT_CONTEXT_PRUNE_TOOLS` 行；
   - 删除 `system_config.json` 的 `agent.context_prune_tools` 键；
   - 使用临时文件加原子替换，保持其他 section、字段和 JSON 编码不变；
3. 文件不存在、agent section 不存在或字段已不存在时视为成功；
4. JSON 无法解析、文件无法安全写回或原子替换失败时才终止部署，不能带着不确定配置继续启动；
5. 新版本后续保存 agent section 时，也只能写入 `_SECTION_FIELDS["agent"]` 中仍存在的字段。

该迁移接入现有 `consumer/deploy/migrate.sh` 流程，不新增独立人工命令。非空遗留值的处理方式是自动删除，不是发布门禁。

#### 4.2.3 回滚语义

- code-only 回滚保留已经清理后的当前 `.onto-runtime` 和 `backend/.env`；旧 backend 因字段和环境变量都缺失，按旧版本默认空值运行，不会重新启用工具名裁剪。
- 标准 `rollback.sh` 会恢复升级前 runtime 和 `backend-env.tar.gz`，属于完整恢复旧版本代码与旧配置的操作，可能恢复旧能力；观察期禁止使用该路径。
- 本次不在 ContextEngine 上实现等价 Processor、ToolResultBudgetProcessor 或其他替代裁剪逻辑。超长工具结果继续作为独立已知边界处理。

### 4.3 ask-user 链路保持不变

以下行为全部保持：

```text
request_user_interaction
    ↓
写入 pending_user_interaction
    ↓
terminal=True，结束当前 agent 执行
    ↓
Orchestrator 将 turn/run 标记为 awaiting_input
    ↓
客户端提交用户回复
    ↓
ExecutionStore 校验并消费 pending interaction
    ↓
用户回复作为新 UserMessage 进入下一次 agent 请求
```

本阶段不使用 Rail 替换这一链路，也不要求 ContextEngine 跨请求保存暂停状态。

## 5. ask-user 的兼容性保护

虽然不切换 ask-user，但窗口管理变化可能影响恢复请求时模型能否看到上一次问询，因此必须做定向回归。

重点验证：

- agent 发起 `request_user_interaction` 后仍正确停止；
- `pending_user_interaction` 和 `awaiting_input` 正确持久化；
- 用户回复仍能在下一次请求中被消费；
- ContextEngine 窗口中保留足够信息，使模型能理解“这是对上一次问询的回复”；
- 工具调用与工具结果不会被拆散；
- `context_max_rounds=1` 时仍能完成问询恢复。

处理原则：

1. openJiuwen 0.1.17 在 `context_max_rounds=1` 时无法原生保留完整问询恢复链，因此不再把原生行为作为待验证分支。
2. 检测到已校验的 `interaction_response` 时，默认 agent 本次运行的 `effective_rounds` 固定至少为 2。
3. 该调整仅影响 ContextEngineConfig，不修改 `request_user_interaction`、Orchestrator 状态机、ExecutionStore 或持久化配置。
4. 主 ReAct `_agent_factory` 路径无法保证同样的轮次策略；只要存在历史消息或 `interaction_response`，本阶段明确拒绝该路径，不保留未定义的 factory 兜底。
5. graph agent 的 factory 路径不消费 ExecutionStore 多轮历史，不参与 ask-user 恢复。
6. 真实 0.1.17 + fake model 测试必须证明 N=1 普通请求仍按 1 轮工作，而 ask-user 恢复请求实际以 2 轮进入模型。

## 6. 实施步骤

### 阶段 A：补齐离线回归基线

先补充或确认以下测试：

- 多轮历史全部传入 ContextEngine，而不是在项目层提前裁剪；
- 使用 `consumer/backend/requirements.txt` 固定安装的真实 openJiuwen 0.1.17 和 fake model，直接捕获 `invoke()` / `stream()` 的 `messages` 参数；
- 普通请求最终按 `configured_rounds` 控制模型窗口；
- ask-user 恢复请求最终按 `max(configured_rounds, 2)` 控制模型窗口；
- tool call / tool result 配对完整；
- 数十轮历史和交错 tool 调用进入 ContextEngine 后，最终窗口仍受轮次配置控制；
- 当前 user message 不重复；
- `before_model_call` 明确仍是 preview，不能通过该回调断言最终窗口；
- `iteration_prepared` 明确是 run 起始候选快照，不是每轮窗口；
- 主 `_agent_factory` 有历史或 `interaction_response` 时被拒绝，无历史单轮调用允许；
- graph agent factory 不被误标为完成了多轮 ContextEngine 裁剪；
- ask-user 中断、持久化和恢复链路不变；
- 旧 system config 中遗留 `context_prune_tools` 时可正常启动，且 effective/public config 不再暴露该字段；
- 新配置接口提交 `context_prune_tools` 时明确拒绝；
- 配置迁移 dry-run 能发现旧 JSON/env 遗留项，正式迁移能幂等删除并保持其他配置不变；
- 清理后的当前 runtime/env 使用旧 backend 启动时，旧配置解析结果仍为空；
- 新版本进入 `awaiting_input` 后，使用旧 backend 代码但保留当前 `.onto-runtime`，用户回复仍能被消费。

这里是离线测试和本地验证，不在生产请求中同时运行两套窗口。

### 阶段 B：直接切换窗口所有权

在 `jiuwen_engine.py` 中同时移除历史构建和观测候选消息构建中的项目层窗口裁剪，让 ContextEngine 直接接收完整历史，并修正相关观测语义和 `_agent_factory` 能力契约。

切换后只保留一条运行链路：

```text
full history → ContextEngine → ReAct
```

不增加 `shadow` 状态，也不执行双结果对比。

### 阶段 C：执行定向回归

测试环境以 `consumer/backend/requirements.txt` 为唯一依赖来源，其中已经固定 `openjiuwen==0.1.17`。

Windows 本地只执行 backend 行为测试：

```powershell
cd consumer/backend
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pip install pytest
$env:PYTHONPATH = "."
.\venv\Scripts\python.exe -m pytest `
  tests/test_context_engine_model_window.py `
  tests/test_jiuwen_react_adapter.py `
  tests/test_orchestrator_runtime.py `
  tests/test_jiuwen_runtime_cleanup.py `
  tests/test_graph_agent.py `
  tests/test_context_engine_cutover_probe.py -q
```

其中 `test_context_engine_model_window.py` 必须使用 requirements 安装的真实 openJiuwen 0.1.17 和 fake model，直接断言模型最终收到的 messages。

deploy、权限和 rollback E2E 只在 Linux CI 或 Linux 容器中作为门禁执行：

```bash
cd consumer/backend
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip install pytest

cd ..
PYTHONPATH=backend backend/venv/bin/python -m pytest \
  deploy/tests/test_context_engine_code_rollback.py \
  deploy/tests/test_release_compose.py \
  deploy/tests/test_migrate_auth_workspace.py -q
```

`test_migrate_auth_workspace.py` 的 POSIX `0600` 权限断言不作为 Windows 门禁；Linux CI 上上述命令必须全绿。最后再执行完整 backend 测试。

### 阶段 D：预发布环境验证

当前 release compose 只有一个 backend，现有 `deploy.sh` 会停止旧栈后整体启动新栈，不具备生产实例级灰度能力。因此本方案不再使用含糊的“小范围发布”表述。

默认发布方式：

1. 在独立预发布环境部署完整新版本；
2. 预发布连续观察至少 24 小时；
3. 完成至少 100 个 main-agent turn，其中至少包含：
   - 30 个带工具调用的 turn；
   - 10 个 ask-user 发起和恢复流程；
   - 5 个 `context_max_rounds=1` 的 ask-user 恢复流程；
   - 10 个十轮以上历史的请求；
4. 所有工具调用配对错误、ask-user 恢复失败和上下文结构异常均必须为 0；
5. 新增随发布包分发的 `consumer/deploy/context_engine_cutover_probe.py`，使用隔离 session 执行固定 synthetic cases，并输出 JSON：
   - `case_count`、`passed_count`、`failed_count`；
   - `tool_pair_error_count`；
   - `ask_user_resume_failure_count`；
   - 每个固定 case 的 `prompt_tokens_max`；
   - 基于现有 performance aggregate 的 run `success_rate`、`total_duration_ms.p90/p99`、`llm_wall_ms.p90/p99`；
6. 相对同一 synthetic case 集合的旧版本基线：
   - run `success_rate` 不得下降超过 1 个百分点；
   - `total_duration_ms.p90` 和 `llm_wall_ms.p90` 不得增加超过 15%；
   - `total_duration_ms.p99` 和 `llm_wall_ms.p99` 不得增加超过 20%；
   - 每个固定 case 的 `prompt_tokens_max` 不得增加超过 10%；
   - `tool_pair_error_count`、`ask_user_resume_failure_count` 必须为 0。

最终窗口正确性只由真实 openJiuwen 0.1.17 + fake model 自动化测试确认，生产环境不记录消息正文。

probe 分为 `baseline` 和 `verify` 两种模式：

- `baseline` 在旧版本仍运行时生成基线文件；
- `verify` 必须传入 `--baseline`，自动比较全部阈值；
- `--policy preprod|production` 选择本方案规定的阈值集合，比较逻辑在脚本内完成；
- 达标退出码为 0；功能失败退出 2；指标或 token 缺失退出 3；配置/执行错误退出 4；
- 每次运行生成 `{prefix}-{UTC timestamp}-{uuid}` 唯一 session id，并记录本次产生的全部 run id；
- 聚合时只能按本次 session id 和 run id 白名单读取，不允许扫描同前缀的历史运行；
- 任一固定 case 缺少 `prompt_tokens_max` 时直接失败，不能跳过 token 门禁。

`tool_pair_error_count` 必须从本次 ExecutionStore 最终消息计算：

1. 收集 assistant message 中全部 `tool_calls[].id`；
2. 收集 tool message 的 `tool_call_id`；
3. 每个 call id 必须且只能对应一个 tool result；
4. 不允许无 assistant call 的孤立 tool result；
5. tool message 存在 name 时，必须与对应 call 的 function name 一致；
6. 缺失、重复、孤立或名称不匹配各计为一个结构错误。

`ask_user_resume_failure_count` 必须验证完整状态链：

```text
request_user_interaction
→ turn/run awaiting_input
→ pending interaction 可读取
→ interaction_response 校验通过
→ pending 被消费并记录 interaction_consumed_at
→ 后续 turn 成功或进入预期的下一业务状态
```

旧版本 baseline 生成命令固定为：

```bash
cd /opt/onto-agent
docker cp /tmp/onto-agent/deploy/context_engine_cutover_probe.py \
  onto-agent-backend:/tmp/context_engine_cutover_probe.py
docker exec onto-agent-backend \
  python /tmp/context_engine_cutover_probe.py baseline \
  --case-set context-engine-cutover \
  --session-prefix ce-baseline \
  --output /app/.onto-runtime/reports/context-engine-cutover-baseline.json
```

新版本预发布验证命令固定为：

```bash
cd /opt/onto-agent
docker cp deploy/context_engine_cutover_probe.py \
  onto-agent-backend:/tmp/context_engine_cutover_probe.py
docker exec onto-agent-backend \
  python /tmp/context_engine_cutover_probe.py verify \
  --case-set context-engine-cutover \
  --policy preprod \
  --session-prefix ce-preprod \
  --baseline /app/.onto-runtime/reports/context-engine-cutover-baseline.json \
  --output /app/.onto-runtime/reports/context-engine-cutover-preprod.json
```

本次不新增 p95 统计，也不新增 local exporter 的 LLM failure 事件映射；发布门禁只使用当前可以计算的 p90/p99、run success rate 和逐 case `prompt_tokens_max`。

### 阶段 E：生产全量切换

如果不新增独立实例和外部路由，生产发布就是全量切换，必须安排维护窗口。

生产切换后：

- 前 2 小时主动观察；
- 持续观察满 24 小时后再关闭发布事件；
- 任一 ask-user 恢复失败、tool call/result 结构错误或上下文构建异常立即回滚；
- 在 T+0、T+60 分钟、T+120 分钟各执行一次 production synthetic probe，三个结果的结构错误计数必须全部为 0；
- 连续 15 分钟且样本数不少于 30 时，若 run success rate 相对基线下降超过 2 个百分点，或现有 `total_duration_ms.p90/p99`、`llm_wall_ms.p90/p99` 任一超过预发布门禁上限，执行 code-only 回滚；
- production synthetic probe 中任一固定 case 的 `prompt_tokens_max` 相对基线增加超过 15%，执行 code-only 回滚；
- 复核现有面板和告警，确保 `iteration_prepared` 指标没有继续被解释为实际或逐轮模型窗口。

生产 probe 命令固定为：

```bash
cd /opt/onto-agent
docker cp deploy/context_engine_cutover_probe.py \
  onto-agent-backend:/tmp/context_engine_cutover_probe.py
docker exec onto-agent-backend \
  python /tmp/context_engine_cutover_probe.py verify \
  --case-set context-engine-cutover \
  --policy production \
  --session-prefix ce-production \
  --baseline /app/.onto-runtime/reports/context-engine-cutover-baseline.json \
  --output /app/.onto-runtime/reports/context-engine-cutover-production.json
```

## 7. 预计修改文件

必改：

- `consumer/backend/agent/core/jiuwen_engine.py`
- `consumer/backend/agent/context.py`
- `consumer/backend/agent/core/jiuwen/trace.py`
- `consumer/backend/agent/tools/business_data/providers/graph/jiuwen_agent.py`
- `consumer/backend/config/settings.py`
- `consumer/backend/config/system_config.py`
- `consumer/frontend/src/components/SystemSettings.jsx`
- `consumer/deploy/migrate.sh`
- `consumer/deploy/migrate_system_config.py`（新增，由 `migrate.sh` 调用）
- `consumer/deploy/tests/test_migrate_system_config.py`（新增）
- `consumer/deploy/context_engine_cutover_probe.py`
- `consumer/backend/tests/test_agent_context.py`
- `consumer/backend/tests/test_system_config_settings.py`
- `consumer/backend/tests/test_context_engine_model_window.py`
- `consumer/backend/tests/test_context_engine_cutover_probe.py`
- `consumer/backend/tests/test_jiuwen_react_adapter.py`
- `consumer/backend/tests/test_jiuwen_runtime_cleanup.py`
- `consumer/backend/tests/test_graph_agent.py`
- `consumer/backend/tests/test_agent_runtime_config.py`
- `consumer/deploy/common.sh`
- `consumer/deploy/deploy.sh`
- `consumer/deploy/upgrade.sh`
- `consumer/deploy/restart.sh`
- `consumer/deploy/rollback.sh`
- `consumer/deploy/rollback-code-only.sh`
- `consumer/deploy/tests/test_context_engine_code_rollback.py`
- `consumer/deploy/tests/test_release_compose.py`
- `consumer/backend/.env.example`
- `consumer/docs/api.md`
- `consumer/backend/README.md`
- `consumer/backend/README-CN.md`
- `consumer/deploy/README.md`

可能补充测试，但原则上不改运行逻辑：

- Orchestrator 的 ask-user 测试；
- ExecutionStore 的 pending interaction 测试；
- consumer/backend 的端到端恢复测试。

原则上不改：

- `request_user_interaction` 工具实现；
- Orchestrator 的状态机；
- ExecutionStore；
- SDK；前端只删除 `context_prune_tools` 设置入口，不改其他交互；
- OpenJiuwen Rail 配置。

## 8. 测试矩阵

| 场景 | 预期 |
|---|---|
| 单轮普通问答 | 与当前行为一致 |
| 多轮普通问答 | ContextEngine 按配置保留最近 N 轮 |
| 多轮工具调用 | 项目历史转换和 ReAct 写入保持 tool call/result 配对，ContextEngine 不从孤立 ToolMessage 开头返回窗口 |
| 数十轮历史 + 交错 tool 调用 | ContextEngine 输出仍受 `default_window_round_num` 控制，tool call / result 成对 |
| 超长工具结果 | 只验证消息结构不被裁坏；不承诺单个保留轮次一定低于真实模型 token 上限 |
| 当前 user message | history 与 invocation input 中不重复 |
| run 起始候选输入 | `iteration_prepared` 是一次性候选快照，不解释为最终或逐轮窗口 |
| `before_model_call` | 仅为裁剪前 preview |
| 实际模型窗口 | 真实 0.1.17 + fake model 捕获的模型调用参数符合预期 |
| `sliding_window_applied` | 兼容字段；True 仅表示 Jiuwen ContextEngine 轮次窗口已由项目配置，不表示项目层已裁剪消息，也不作为窗口正确性证据 |
| 主 factory + 历史或 interaction response | 明确拒绝，避免无法保证轮次窗口 |
| 主 factory + 无历史 | 允许单轮执行，静态标识为 False |
| graph factory agent | 作为独立单轮链路处理，不声称完成多轮裁剪 |
| ask-user 发起 | 当前执行终止并进入 `awaiting_input` |
| ask-user 回复 | pending interaction 被消费，执行可继续 |
| 普通请求 `context_max_rounds=1` | 模型最终只接收最近 1 轮 |
| ask-user 恢复 `context_max_rounds=1` | 本次 effective rounds 为 2，模型接收完整问询链 |
| 旧 system config 遗留 `context_prune_tools` | 正常启动；字段不生效且不出现在 effective/public config |
| 旧 backend env 遗留 `AGENT_CONTEXT_PRUNE_TOOLS` | 新 backend 不读取；升级迁移自动删除 |
| 新配置接口提交 `context_prune_tools` | 按未知字段拒绝 |
| 配置迁移重复执行 | 幂等成功，不修改其他配置 |
| 清理后 code-only 回滚到旧 backend | 旧 backend 读取到默认空值，不重新启用工具名裁剪 |
| ContextEngine 异常 | 错误显式暴露，不静默切换到第二套窗口 |
| 标准 `rollback.sh` | 不用于本次观察期回滚，因为会恢复旧 `.onto-runtime` |
| code-only 回滚 | 旧 backend 代码使用当前 `.onto-runtime`，待恢复问询仍可继续 |
| code-only 回滚失败 | 自动恢复原 backend/mcp，state 不写入混合状态 |
| 混合状态 restart | 按 `CURRENT_*_IMAGE` 恢复相同服务组合 |
| 混合状态再次升级 | 快速拒绝；必须先 `--restore-original` |
| 混合状态标准 rollback | 明确拒绝执行 |
| 旧格式 current.env | 按 normal 和 `CURRENT_VERSION` 默认化，脚本不因 `set -u` 失败 |
| probe baseline | 唯一 session/run 集合、指标和 token 完整，否则非零退出 |
| probe verify | 自动读取 baseline、比较 policy 阈值并以退出码表达结果 |

## 9. 回退方案

现有 `consumer/deploy/rollback.sh` 会删除当前 `.onto-runtime` 并恢复升级前快照，不能用于 ContextEngine 切换观察期的首选回滚，否则新增会话和 ask-user checkpoint 会从在线状态消失。

发布前必须新增 `consumer/deploy/rollback-code-only.sh`，职责为：

- 通过 scoped compose helper 只停止 `mcp`、`backend`，保持当前 frontend 和 auth-server；
- 创建当前 `.onto-runtime` 的 failsafe backup；
- 加载并启动上一版本 backend 镜像；
- 保持当前 `.onto-runtime`、`.onto-auth` 和当前环境配置不变；
- 不执行 `restore_archive_if_present`；
- backend/mcp 健康后完成状态记录；
- 失败时自动恢复原 backend/mcp；原 backend 也无法恢复时保留 failsafe backup 并明确退出。

`common.sh` 必须新增 scoped helper：

- `compose_stop_services <images...> mcp backend`：执行 `docker compose stop`，不执行整个 stack 的 `down`；
- `compose_up_services <images...> backend mcp`：执行 `up -d --no-deps --force-recreate`；
- code-only 脚本不能调用现有整栈 `compose_down()` / `compose_up()`。

上一 backend 镜像的解析顺序固定为：

1. state 中的 `PREVIOUS_BACKEND_IMAGE`；
2. `LAST_BACKUP_DIR/metadata.env` 中升级前的 `CURRENT_BACKEND_IMAGE`；
3. 如本地不存在该镜像，从 `LAST_BACKUP_DIR/backend-image.tar` 执行 `docker load`；
4. 任一步仍无法得到唯一镜像时立即失败，不使用模糊的本地 tag 猜测。

### 9.1 code-only 状态契约

为表达“旧 backend + 当前 frontend/auth/runtime”的混合状态，`current.env` 增加：

```text
DEPLOYMENT_MODE=normal|backend_code_rollback
RUNTIME_VERSION=
CURRENT_BACKEND_VERSION=
CURRENT_FRONTEND_VERSION=
CURRENT_AUTH_VERSION=
CODE_ROLLBACK_ORIGINAL_BACKEND_IMAGE=
CODE_ROLLBACK_ORIGINAL_BACKEND_VERSION=
CODE_ROLLBACK_BACKUP_DIR=
```

`load_state` 必须兼容不包含这些字段的旧 `current.env`。在 source 后立即执行默认化，避免 `set -u`：

```bash
DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-normal}"
RUNTIME_VERSION="${RUNTIME_VERSION:-${CURRENT_VERSION:-}}"
CURRENT_BACKEND_VERSION="${CURRENT_BACKEND_VERSION:-${CURRENT_VERSION:-}}"
CURRENT_FRONTEND_VERSION="${CURRENT_FRONTEND_VERSION:-${CURRENT_VERSION:-}}"
CURRENT_AUTH_VERSION="${CURRENT_AUTH_VERSION:-${CURRENT_VERSION:-}}"
CODE_ROLLBACK_ORIGINAL_BACKEND_IMAGE="${CODE_ROLLBACK_ORIGINAL_BACKEND_IMAGE:-}"
CODE_ROLLBACK_ORIGINAL_BACKEND_VERSION="${CODE_ROLLBACK_ORIGINAL_BACKEND_VERSION:-}"
CODE_ROLLBACK_BACKUP_DIR="${CODE_ROLLBACK_BACKUP_DIR:-}"
```

旧 state 经 `load_state` 后视为 normal；`restart.sh`、`deploy.sh` 和标准 `rollback.sh` 必须有旧 state 兼容测试。

正常部署成功后：

```text
DEPLOYMENT_MODE=normal
CURRENT_VERSION=目标发布版本
RUNTIME_VERSION=目标发布版本
CURRENT_BACKEND_VERSION=目标发布版本
CURRENT_FRONTEND_VERSION=目标发布版本
CURRENT_AUTH_VERSION=目标发布版本
CODE_ROLLBACK_*=
```

code-only 回滚成功后：

```text
DEPLOYMENT_MODE=backend_code_rollback
CURRENT_VERSION=本次新发布版本
RUNTIME_VERSION=本次新发布版本
CURRENT_BACKEND_VERSION=上一发布版本
CURRENT_BACKEND_IMAGE=上一 backend 镜像
CURRENT_FRONTEND_VERSION=本次新发布版本
CURRENT_FRONTEND_IMAGE=当前 frontend 镜像
CURRENT_AUTH_VERSION=本次新发布版本
CURRENT_AUTH_IMAGE=当前 auth 镜像
PREVIOUS_VERSION=保持原值
PREVIOUS_BACKEND_IMAGE=保持原值
PREVIOUS_FRONTEND_IMAGE=保持原值
PREVIOUS_AUTH_IMAGE=保持原值
LAST_BACKUP_DIR=保持原值
CODE_ROLLBACK_ORIGINAL_BACKEND_IMAGE=回滚前的新 backend 镜像
CODE_ROLLBACK_ORIGINAL_BACKEND_VERSION=本次新发布版本
CODE_ROLLBACK_BACKUP_DIR=code-only 前的 failsafe backup
```

状态只在旧 backend 和 mcp 全部健康后原子写入。回滚过程中失败时 state 保持原正常状态，脚本使用内存中保存的原 backend 镜像恢复 backend/mcp。

### 9.2 后续命令对混合状态的解释

- `restart.sh`：按照 `CURRENT_*_IMAGE` 重启当前混合组合，不改变 `DEPLOYMENT_MODE`。
- 再次升级：`upgrade.sh` 和 `deploy.sh` 检测到 `DEPLOYMENT_MODE=backend_code_rollback` 时必须拒绝；操作者必须先执行 `rollback-code-only.sh --restore-original` 回到 normal，再进行升级。
- 标准 `rollback.sh`：检测到 `DEPLOYMENT_MODE=backend_code_rollback` 时拒绝执行，避免误恢复旧 `.onto-runtime`。
- 恢复原新 backend：执行 `rollback-code-only.sh --restore-original`，使用 `CODE_ROLLBACK_ORIGINAL_BACKEND_IMAGE` 重建 backend/mcp；成功后 `CURRENT_BACKEND_VERSION=CURRENT_VERSION`、`DEPLOYMENT_MODE=normal` 并清空 `CODE_ROLLBACK_*`。
- 若业务明确要求连数据一起恢复，必须先恢复到 normal 状态，再单独执行标准 `rollback.sh`，并再次确认其会恢复旧 runtime 快照。

### 9.3 非法状态转换

所有状态和镜像校验必须发生在停止容器之前。以下情况快速拒绝，且不得改变容器或 state：

- normal 状态执行 `--restore-original`；
- mixed 状态重复执行普通 code-only 回滚；
- mixed 状态执行 `upgrade.sh`、`deploy.sh` 或标准 `rollback.sh`；
- `DEPLOYMENT_MODE` 为未知值；
- 上一 backend 镜像或 original backend 镜像无法唯一解析；
- state 必填镜像字段缺失；
- failsafe backup 无法创建。

发布时唯一允许的观察期回滚命令为：

```bash
cd /opt/onto-agent
./deploy/rollback-code-only.sh
```

该脚本当前尚不存在，是发布阻断项，不能用标准 `rollback.sh` 代替。

必须增加 Linux-only 的真实 Docker Compose 双镜像 E2E。测试使用两个可识别版本的 backend 测试镜像、真实 bind-mounted 临时 `.onto-runtime` 和临时 state 文件，不允许只 mock shell 函数。

```text
新版本发起 request_user_interaction
→ 状态进入 awaiting_input 并写入当前 .onto-runtime
→ 执行 code-only 回滚
→ 旧 backend 镜像挂载同一份当前 .onto-runtime
→ 用户提交 interaction_response
→ pending interaction 被消费并继续执行
```

`consumer/deploy/tests/test_context_engine_code_rollback.py` 至少覆盖：

1. happy path：旧 backend + 当前 runtime 能消费新版本创建的 pending interaction；
2. 旧 backend 健康检查失败：自动恢复 original backend/mcp，state 保持 normal；
3. mixed-state restart：重启后仍是旧 backend + 当前 frontend/auth/runtime；
4. mixed-state upgrade：`upgrade.sh` / `deploy.sh` 快速拒绝，容器和 state 不变；
5. mixed-state standard rollback：快速拒绝，runtime 不恢复旧快照；
6. `--restore-original`：恢复新 backend 后归一为 normal；
7. normal 状态执行 `--restore-original`：拒绝；
8. mixed 状态重复执行普通 code-only 回滚：拒绝；
9. 上一 backend 镜像缺失且 backup 也无法提供：在停止容器前拒绝；
10. 旧格式 `current.env`：deploy/restart/rollback 按 normal 默认值工作；
11. 未知 `DEPLOYMENT_MODE` 或必填 state 字段缺失：拒绝且不改变任何容器/state。

测试变更、窗口切换和 code-only 回滚脚本应分别提交，便于独立审查和回退。

## 10. 明确延期的事项

以下内容进入后续独立议题：

- 使用 AskUserRail 替换 `request_user_interaction`；
- 使用 Checkpointer 实现 OpenJiuwen 原生中断恢复；
- ContextEngine 跨请求持久化；
- 摘要、压缩、卸载和自定义 Processor；
- ContextEngine 与 ExecutionStore 的长期状态整合；
- 清理项目旧滑窗实现。

这些事项都不应成为本次 ContextEngine 窗口切换的前置条件。

已知边界：

- `default_window_round_num` 按轮次裁剪，不保证单个被保留轮次一定小于模型 token 上限；
- 单个超长 tool result 可能使最终窗口超过真实模型上下文限制；
- 本次测试只保证结构完整和行为显式失败，不实现工具结果摘要、截断或 token-aware Processor；
- 如生产存在超长工具结果导致的模型上下文溢出，应作为独立优化项处理，不能用恢复项目层通用滑窗掩盖。

## 11. 最终实施顺序

```text
1. 扩展 current.env 状态契约和 common.sh scoped compose helper
2. 实现 rollback-code-only.sh、--restore-original 和混合状态命令保护
3. 在 Linux CI 完成 code-only 回滚及 awaiting_input 恢复 E2E
4. 删除 context_prune_tools 的后端、前端、运行时行为和文档契约
5. 将旧 JSON/env 遗留项的幂等清理接入现有 migrate.sh，并验证 code-only 回滚不会重新激活
6. 在 _create_agent 前统一计算真实历史、interaction_response 和 effective_rounds
7. 用 requirements 固定的真实 openJiuwen 0.1.17 + fake model 建立最终模型入参测试
8. 新增 synthetic probe，并使用现有 p90/p99、run success rate 和逐 case prompt_tokens_max
9. 同时移除历史构建和 run 起始候选估算中的项目滑窗
10. 将 sliding_window_applied 改为 trace bridge 必传参数并修正所有调用点
11. 验证长历史、交错工具调用、ask-user 恢复和超长工具结果已知边界
12. 执行 Windows backend 定向测试、Linux deploy 门禁和完整 backend 回归
13. 在独立预发布环境完成 24 小时和规定样本量验证
14. 在维护窗口执行生产全量切换并观察 24 小时
15. 稳定后再单独评估 Rail 问询机制
```

## 12. 结论

ContextEngine 优先、保留现有 ask-user 状态机的主体设计可以通过并进入实现，但当前方案仍不能标记为 **Final Approved**，最终发布前必须完成下述阻断项和门禁。

最稳妥的边界是：

- ContextEngine 接管当前 LLM 窗口；
- ExecutionStore 继续管理完整历史和业务状态；
- `request_user_interaction` 继续使用现有 terminal/checkpoint 流程；
- 不做影子运行；
- 使用真实 0.1.17 模型入参测试验证最终窗口；
- ask-user 恢复请求的有效轮次至少为 2；
- `context_prune_tools` 能力和配置契约完整删除；旧配置可读取但不生效，并在升级迁移中自动清理；
- 通过独立预发布环境验证后执行生产全量切换；
- 观察期只使用保留当前 `.onto-runtime` 的 code-only 回滚。

当前剩余的 P0 阻断项是：

1. 按本方案实现完整的 mixed-state 契约、scoped compose helper、`rollback-code-only.sh`、失败恢复及 Linux E2E。

进入最终发布批准前还必须完成三个 P1：

1. 落地可执行的 synthetic probe，并只使用当前可计算的指标；
2. 使用 requirements 标准环境和 Linux deploy 门禁，确保精确测试命令全绿；
3. 在 `_create_agent` 前按真实历史口径完成 factory 拒绝和 effective rounds 计算。

上述事项闭环后，本方案才适合作为最终发布执行方案。
