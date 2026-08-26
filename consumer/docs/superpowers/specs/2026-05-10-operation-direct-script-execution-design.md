# 本体 Operation 直接脚本执行分支设计方案

状态：评审稿

日期：2026-05-10

适用项目：onto-agent

## 1. 背景

当前项目是本体驱动问答系统。用户问题通过 `POST /agentic/runs` 创建 Run 和 session Turn，服务端返回身份后，客户端用 `GET /agentic/runs/{run_id}/events?after_seq=0` 读取 SSE。原生异步 Runtime 随后进入 Jiuwen ReAct loop；主循环通过模型匹配 ontology、匹配 operation、读取本体文档、调用工具，最终生成回答。

当前系统已经具备脚本型 operation 的执行能力：

- 本体目录下可以包含 `scripts-list.md` 和 `scripts/`。
- `scripts-list.md` 维护 operation 到脚本文件的映射。
- 后端工具 `execute_script` 根据 `ontology_id` 和 `operation_key` 解析脚本路径，校验路径安全，只允许执行 `.py` 脚本。
- Agent prompt 已要求：当 operation 出现在 `script_catalog` 中时，应优先调用 `execute_script`，不要手工复刻 markdown 逻辑。

但这仍然是 Agent-loop 内部的决策。即使命中脚本型 operation，也会先进入模型循环，由模型选择工具，再等待工具结果和最终回答。这带来几个问题：

- 对明确脚本型 operation 的问题，模型规划成本是不必要的。
- 模型可能多读 operation/object 文档，偏离“脚本即工具流程”的语义。
- 最终回答仍依赖模型生成，增加延迟和不确定性。
- 部分脚本执行依赖当前会话上下文，例如 `session_id`，这个值不应从用户自然语言中抽取，也不应由用户或模型伪造。

因此需要新增一条独立分支：当用户问题明确命中脚本型 operation，且所需业务参数和运行时上下文都齐全时，直接执行脚本并返回结果；否则完整回退到现有 Agent-loop。

## 2. 设计目标

本方案目标是在不影响当前 Agent-loop 的前提下，支持本体 operation 的直接脚本执行。

目标：

1. 在 Jiuwen ReAct loop 之前增加一个保守的 direct execution 分支。
2. 复用现有 `execute_script` 脚本执行能力，不产生第二套脚本执行通道。
3. 只有明确命中脚本型 operation 且参数齐全时才绕过 Agent-loop。
4. 将业务参数和运行时参数分离，`session_id` 等运行时参数只能由后端注入。
5. 旧脚本、旧 `scripts-list.md`、旧 Agent-loop 调用方式不受影响。
6. direct 分支继续使用现有 SSE 协议，让前端无需新增一套消息消费模型。
7. 路由不确定、参数缺失、非脚本 operation、多 operation 编排、关系推理类问题全部回退到现有流程。

非目标：

1. 不在第一阶段支持非脚本 operation 的直接执行。
2. 不把 operation markdown 编译成通用 DAG 执行器。
3. 不让用户问题中的 `session_id` 覆盖真实后端会话。
4. 不改变现有 Agent-loop 的工具选择、工具执行和消息持久化语义。
5. 不要求所有旧脚本接收新增参数。
6. 不用环境变量作为主要的 `session_id` 传递机制。

## 3. 核心设计结论

采用“系统开关控制尝试、前置保守路由判断、runtime 占位符注入、原工具复用”的方案。

```text
POST /agentic/runs
        |
        v
202 + server-generated run_id / turn_id
        |
        v
GET /agentic/runs/{run_id}/events?after_seq=0
        |
        v
AsyncExecutionService / Jiuwen Runtime
        |
        +--> direct_execution.enabled ?
        |       |
        |       +--> false -> JiuwenReactLoopEngine.run()
        |       |
        |       +--> true  -> OperationDirectRouter.route()
        |       |
        |       +--> direct_script       -> execute_script -> turn.finished
        |       +--> missing_args        -> turn.finished，提示用户补业务参数
        |       +--> fallback_agent_loop -> JiuwenReactLoopEngine.run()
        |
        v
现有 Agent-loop 保持不变
```

关键原则：

- direct branch 是优化分支，不是替代主链路。
- 是否尝试 direct branch 由后端系统开关控制，不由用户问题参数强制触发。
- 用户请求不新增 `use_direct_execution=true` 这类开关，避免前端或用户绕过后端判断。
- 后端每次只是在 Orchestrator 层做一次轻量前置判断；命中才接管，不命中就进入原 Agent-loop。
- 只有 `scripts-list.md` 明确声明脚本映射的 operation 才允许 direct。
- `session_id` 属于 runtime context，不属于用户业务参数。
- 旧脚本默认不会收到任何新增 CLI 参数。
- 只有脚本映射中显式写了 `<runtime.session_id>`，后端才向该脚本注入真实 `session_id`。

## 4. 总体架构

建议新增独立模块：

```text
backend/agentic/direct_execution/
  __init__.py
  models.py        # RouteDecision / DirectExecutionResult / RuntimeContext
  router.py        # direct branch 入口
  matcher.py       # ontology operation 匹配
  arguments.py     # 业务参数抽取、runtime 占位符识别、参数校验
  runner.py        # 调用 execute_script，包装 SSE 和持久化结果
```

`AgenticOrchestrator` 只感知一个高层能力：

```text
decision = OperationDirectRouter.route(request, context, runtime_context)
```

Jiuwen ReAct loop 不需要知道这个分支存在。

## 5. 关键数据模型

### 5.1 RouteDecision

```json
{
  "route": "direct_script | missing_args | fallback_agent_loop",
  "reason": "clear_script_operation",
  "ontology_id": "example-domain-operations-manual",
  "operation_key": "获取TargetItem供应商编号",
  "business_arguments": {
    "TargetItem编码": "MP-001"
  },
  "missing_business_arguments": [],
  "runtime_requirements": ["session_id"],
  "confidence": 0.96
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `route` | 路由结论 |
| `reason` | 决策原因，便于日志和评审 |
| `ontology_id` | 命中的本体 |
| `operation_key` | 命中的 operation |
| `business_arguments` | 从用户问题或可用会话上下文中提取的业务参数 |
| `missing_business_arguments` | 需要用户补充的业务参数 |
| `runtime_requirements` | 脚本声明需要的运行时参数 |
| `confidence` | 匹配置信度 |

### 5.2 RuntimeContext

```json
{
  "session_id": "sess_123",
  "run_id": "agentic-001",
  "turn_id": 3,
  "user_id": "feng",
  "workspace_id": "example_domain_knowledge",
  "selected_ontology_id": "example-domain-operations-manual"
}
```

`RuntimeContext` 只能由后端构造，来源包括当前请求、orchestrator、run store、session store 和 workspace scope。用户请求体中的自然语言内容不能覆盖这些值。

### 5.3 ScriptInvocation

```json
{
  "ontology_id": "example-domain-operations-manual",
  "operation_key": "获取TargetItem供应商编号",
  "business_arguments": {
    "TargetItem编码": "MP-001"
  },
  "runtime_context": {
    "session_id": "sess_123",
    "run_id": "agentic-001",
    "turn_id": 3,
    "workspace_id": "example_domain_knowledge"
  }
}
```

`ScriptInvocation` 是 direct branch 和 Agent-loop 内部 `execute_script` 都可以复用的统一调用意图。对外工具 schema 仍保持 `arguments` 表示业务参数；runtime 参数由后端内部补齐。

## 6. `scripts-list.md` 参数契约

### 6.1 现有写法保持有效

旧脚本继续使用现有格式：

```md
- `获取TargetItem供应商编号` -> `scripts/get_target_item_supplier.py`
  `args`: `["--target_item-code", "<TargetItem编码>"]`
```

这类脚本不会收到 `session_id`，执行行为保持不变。

### 6.2 新增 runtime 占位符

需要 `session_id` 的脚本显式声明 runtime 参数：

```md
- `基于会话上下文执行某流程` -> `scripts/run_with_session.py`
  `args`: `["--session-id", "<runtime.session_id>", "--target_item-code", "<TargetItem编码>"]`
```

占位符规则：

| 占位符 | 来源 | 是否允许用户覆盖 |
|---|---|---|
| `<TargetItem编码>` | 用户问题或会话中的业务输入 | 允许 |
| `<runtime.session_id>` | 当前后端 session_id | 不允许 |
| `<runtime.run_id>` | 当前 run_id | 不允许 |
| `<runtime.turn_id>` | 当前 turn_id | 不允许 |
| `<runtime.user_id>` | 当前用户 id | 不允许 |
| `<runtime.workspace_id>` | 当前 workspace id | 不允许 |
| `<runtime.selected_ontology_id>` | 当前选中 ontology id | 不允许 |

只有 `runtime.` 前缀的占位符才触发后端注入。普通 `<session_id>` 不会被自动当作真实会话 id 注入，避免改变旧脚本语义。

### 6.3 对 Agent 可见的 script catalog

`ontology_context(resource="root")` 返回给 Agent 的 `script_catalog` 应只展示业务参数，避免模型把 runtime 参数当成需要问用户的问题。

内部脚本定义：

```json
{
  "operation_key": "基于会话上下文执行某流程",
  "arguments": ["--session-id", "<runtime.session_id>", "--target_item-code", "<TargetItem编码>"]
}
```

对 Agent 可见：

```json
{
  "operation_key": "基于会话上下文执行某流程",
  "arguments": ["--target_item-code", "<TargetItem编码>"]
}
```

后端可额外在日志或调试接口中记录：

```json
{
  "runtime_requirements": ["session_id"]
}
```

但默认不把 runtime 参数放进 prompt，减少模型误用。

## 7. Direct Route 判断规则

### 7.1 Router 进入条件

`OperationDirectRouter` 不是由用户请求显式指定进入，而是由 Orchestrator 根据系统配置自动尝试。

进入 router 前必须满足：

1. `direct_execution.enabled=true`。
2. 当前请求已经完成 run、session、turn 的基础初始化。
3. 当前后端上下文中存在真实 `session_id`、`run_id`、`turn_id`。
4. 第一阶段要求 `selected_ontology_id` 已存在，除非后续配置明确允许自动 ontology 匹配。

如果 `direct_execution.enabled=false`，Orchestrator 不调用 `OperationDirectRouter`，请求直接进入现有 Agent-loop。

请求体不应暴露强制 direct 的参数，例如：

```json
{
  "context": {
    "use_direct_execution": true
  }
}
```

原因是 direct branch 涉及脚本执行和 runtime 参数注入，是否允许绕过 Agent-loop 必须由后端基于本体、operation、脚本映射和参数完整性判断。

### 7.2 允许 direct 的条件

必须同时满足：

1. 当前请求存在明确 ontology。第一阶段只建议支持 `selected_ontology_id` 已指定的场景。
2. operation 匹配唯一，且置信度达到阈值。
3. 命中的 operation 出现在 `scripts-list.md`。
4. `scripts-list.md` 中声明的业务参数都已获得。
5. runtime 占位符所需字段都能从后端上下文获得。
6. 用户意图是执行、查询、计算、判断类请求。
7. 请求不需要多 operation 编排、不需要关系推理、不需要解释 operation 逻辑。

### 7.3 必须 fallback 的情况

出现任一情况，直接回退 Agent-loop：

- 没有 `selected_ontology_id`，且无法用确定性规则唯一选中 ontology。
- 多个 operation 分数接近。
- 命中的是非脚本 operation。
- 问题是“解释流程”“为什么”“影响路径”“上下游关系”“比较方案”等推理类意图。
- 用户要求综合多个 operation。
- 业务参数抽取不确定。
- direct router 内部异常。

### 7.4 业务参数缺失

如果 operation 和脚本都明确，但业务参数缺失，direct branch 可以直接返回缺失参数，不进入 Agent-loop。

示例：

```text
当前工具流程需要以下参数：
- TargetItem编码

请补充后我再执行。
```

这类缺失属于用户可补充信息。

### 7.5 runtime 参数缺失

如果 `<runtime.session_id>` 等运行时参数缺失，不应询问用户。

处理策略：

- `session_id` 缺失：系统错误，因为 `POST /agentic/runs` 要求 session_id。
- `workspace_id` 缺失：如果脚本声明需要，则系统错误或 fallback，根据该字段是否必需。
- `user_id` 缺失：认证链路异常。

runtime 参数缺失说明后端上下文不完整，不是用户问题不完整。

## 8. Operation 匹配策略

### 8.1 第一阶段：确定性匹配

第一阶段只做低风险匹配，避免新增模型调用。

候选来源：

- 当前 selected ontology 的 `operations-list.md`。
- 当前 selected ontology 的 `scripts-list.md`。

匹配信号：

- operation 名称完整出现在用户问题中。
- operation 名称的关键词与用户问题高度重合。
- 用户问题中出现脚本参数所需的业务值。
- operation 标签为工具流程或存在脚本映射。

只在唯一候选明显领先时 direct。否则 fallback。

### 8.2 第二阶段：轻量 preflight

如果后续需要提升召回，可以引入 `task_llm` 做严格 JSON preflight。

约束：

- preflight 只输出候选 operation、业务参数、缺失参数和置信度。
- preflight 不能输出 runtime 参数值。
- preflight 不能决定覆盖后端 runtime context。
- preflight 低于阈值必须 fallback。

第一阶段不必实现 preflight，避免一开始就增加模型调用。

## 9. 脚本执行参数构建

现有 `_build_cli_args` 可以演进为三类解析：

```text
script_args + business_arguments + runtime_context -> cli_args
```

解析规则：

1. 普通 flag 原样保留，例如 `--target_item-code`。
2. 普通占位符从 `business_arguments` 填充，例如 `<TargetItem编码>`。
3. `runtime.` 占位符从 `runtime_context` 填充，例如 `<runtime.session_id>`。
4. 用户传入的 `arguments` 中即使包含 `session_id`，也不能用于填充 `<runtime.session_id>`。
5. 未声明 runtime 占位符的脚本，不会额外收到 runtime 参数。
6. runtime 占位符缺失时返回系统错误，不加入 missing business arguments。

示例：

```md
`args`: `["--session-id", "<runtime.session_id>", "--target_item-code", "<TargetItem编码>"]`
```

输入：

```json
{
  "business_arguments": {
    "TargetItem编码": "MP-001"
  },
  "runtime_context": {
    "session_id": "sess_123"
  }
}
```

输出 CLI：

```text
["--session-id", "sess_123", "--target_item-code", "MP-001"]
```

## 10. 与现有 Agent-loop 的关系

### 10.1 Orchestrator 改造点

`AgenticOrchestrator.run()` 在完成以下现有步骤后尝试 direct route：

1. 校验并规范化 `session_id`。
2. 解析 `selected_ontology_id`。
3. 创建或读取 session。
4. 创建 run。
5. 调用 `execution_store.start_turn`。
6. 调用 `chat_store.add_message`。
7. 发出 `turn.started`。

之后：

```text
if direct_execution.enabled is false:
    进入现有 Jiuwen ReAct loop
    return when Jiuwen ReAct loop finishes

decision = OperationDirectRouter.route(request, context, runtime_context)

if decision.route == "direct_script":
    执行 direct script
    持久化 final_output
    complete run / turn
    发送 turn.finished
    return

if decision.route == "missing_args":
    持久化缺失参数提示
    complete run / turn
    发送 turn.finished
    return

if decision.route == "fallback_agent_loop":
    进入现有 Jiuwen ReAct loop
```

这样 direct branch 与 Agent-loop 共享 run/session/turn 生命周期。开关关闭时，`OperationDirectRouter` 不参与请求；开关开启但未命中时，请求继续走原 Agent-loop。

### 10.2 Agent-loop 不改

Jiuwen ReAct loop 保持当前行为：

- 继续构建 system prompt。
- 继续让模型调用 `ontology_context`、`business_data_search`、`execute_script`。
- 继续使用 `execute_tool_batch`。
- 继续持久化完整 LLM context。

如果 direct route fallback，用户不会感知到前置路由存在。

### 10.3 `execute_script` 复用

direct branch 不直接 `subprocess.run`。

推荐调用现有 `ExecuteScriptTool` 或其底层 `handle_execute_script`，并传入：

- `ontology_id`
- `operation_key`
- `arguments`，只包含业务参数
- `runtime_context`，由后端内部参数传入
- `run_id`
- `session_id`
- `turn_id`
- `caller="direct"`

如果现有 `ToolCaller` 类型只支持 `main` 和 `subagent`，可以不新增 caller 类型，先在日志字段中标识 `execution_path="direct"`，避免扩大工具权限模型。

## 11. SSE 协议与前端兼容

direct branch 继续输出 `agent-stream.v2`。

推荐事件序列：

```text
turn.started
tool.started      tool_name=execute_script
tool.finished     tool_name=execute_script
turn.finished     status=success, final_output=脚本结果
```

如果业务参数缺失：

```text
turn.started
turn.finished     status=success, final_output=缺失参数提示
```

如果脚本执行失败：

```text
turn.started
tool.started
tool.finished     status=error
turn.finished     status=success 或 error
```

脚本业务错误和系统错误需要区分：

- 脚本返回业务失败，例如查无数据：作为正常 final_output 返回。
- 脚本进程异常、超时、路径非法：作为工具错误记录，并在 final_output 中说明执行失败。

## 12. 输出格式

脚本输出优先遵循结构化 JSON。

推荐脚本 stdout：

```json
{
  "status": "success",
  "summary": "已完成供应商查询",
  "data": {
    "target_item_code": "MP-001",
    "supplier_code": "SUP-001"
  },
  "warnings": []
}
```

direct branch 不强制二次 LLM 总结。返回策略：

1. stdout 是 JSON：解析后作为结构化结果返回，同时生成简短中文摘要。
2. stdout 是文本：原样返回文本结果。
3. stdout 为空：返回“脚本执行成功，但没有输出”。
4. stderr 非空但 return code 为 0：保留为 warnings 或调试信息，不默认判失败。

第一阶段可以沿用现有 `execute_script` 的输出拼接方式，后续再增强结构化解析。

## 13. 安全边界

### 13.1 用户不能伪造 runtime 参数

以下输入都不能覆盖真实 `session_id`：

- 用户自然语言中写“session_id 是 xxx”。
- 请求 `context` 中携带 `session_id`。
- LLM 工具参数中携带 `session_id`。
- direct router 抽取出的 `business_arguments.session_id`。

只有后端当前 run/session 生命周期中的真实 `session_id` 能填充 `<runtime.session_id>`。

### 13.2 脚本路径安全沿用现有机制

继续保留：

- 禁止绝对路径。
- 禁止 `..` 路径穿越。
- 脚本路径必须位于 project root 内。
- 只允许 `.py`。
- 子进程有超时。
- `PYTHONPATH` 只为脚本运行提供 backend 代码访问能力。

### 13.3 direct route 失败优先 fallback

除脚本已经开始执行的情况外，direct route 内部错误应优先 fallback 到 Agent-loop，避免新分支影响主流程可用性。

脚本一旦开始执行，不应再 fallback 到 Agent-loop 自动重试，避免副作用重复执行。

## 14. 可观测性

新增 perf log：

```text
direct_execution.route.started
direct_execution.route.completed
direct_execution.script.started
direct_execution.script.completed
direct_execution.fallback
direct_execution.missing_args
direct_execution.runtime_context_missing
```

关键字段：

| 字段 | 说明 |
|---|---|
| `run_id` | 当前 run |
| `session_id` | 当前 session |
| `turn_id` | 当前 turn |
| `workspace_id` | 当前 workspace |
| `ontology_id` | 命中 ontology |
| `operation_key` | 命中 operation |
| `route` | direct / missing_args / fallback |
| `reason` | 决策原因 |
| `confidence` | 置信度 |
| `missing_business_arguments` | 缺失业务参数 |
| `runtime_requirements` | runtime 参数需求 |
| `duration_ms` | 耗时 |

日志中可以记录 runtime 参数是否存在，但不应泄露敏感值。`session_id` 可以按现有日志策略记录完整值或脱敏值，最终以项目日志规范为准。

## 15. 配置开关

需要新增后端系统配置开关。这个开关用于发布、灰度和回滚，不是用户每次提问时选择执行路径的参数。

```json
{
  "direct_execution": {
    "enabled": false,
    "selected_ontology_only": true,
    "confidence_threshold": 0.85,
    "allow_task_llm_preflight": false
  }
}
```

默认关闭，避免发布后改变现有行为。

字段说明：

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `enabled` | `false` | 是否启用 direct branch |
| `selected_ontology_only` | `true` | 第一阶段只在前端已选 ontology 时启用 |
| `confidence_threshold` | `0.85` | direct 匹配阈值 |
| `allow_task_llm_preflight` | `false` | 是否允许小模型 preflight |

配置语义：

```text
direct_execution.enabled=false
-> Orchestrator 不调用 OperationDirectRouter
-> 当前 Agent-loop 流程保持不变

direct_execution.enabled=true
-> Orchestrator 自动尝试 OperationDirectRouter
-> 命中 direct_script 才绕过 Agent-loop
-> 未命中或不确定时回退 Agent-loop
```

第一阶段建议只通过后端配置开启，不增加前端开关，也不在普通请求体中加入 `use_direct_execution`。如果需要排查路由行为，可以后续增加只读诊断字段，例如 `direct_execution_debug=true`，但该字段只能影响日志或调试输出，不能强制进入 direct script。

## 16. 分阶段实施

### 阶段一：保守直执行

范围：

- 新增 direct execution 模块。
- 只支持 selected ontology。
- 只支持确定性 operation 匹配。
- 支持 `<runtime.session_id>` 注入。
- direct branch 复用 `execute_script`。
- 默认配置关闭。

验收标准：

- 未开启配置时，所有现有流程和测试结果不变。
- 旧脚本没有 runtime 占位符时，不会收到新增 CLI 参数。
- 显式声明 `<runtime.session_id>` 的脚本能收到真实 session_id。
- 用户传入假的 session_id 无法覆盖真实值。

### 阶段二：增强匹配和结构化输出

范围：

- 增加 operation alias 或关键词匹配。
- 支持脚本 stdout JSON 解析。
- 增强缺失参数提示格式。
- 增加 direct branch 的前端展示细节。

验收标准：

- direct 命中率提升，但误命中率保持可控。
- 文本输出和 JSON 输出都能稳定展示。

### 阶段三：轻量 preflight

范围：

- 可选启用 `task_llm` preflight。
- 只用于 operation 候选和业务参数抽取。
- 不允许 preflight 输出 runtime 参数值。

验收标准：

- 低置信度全部 fallback。
- 成本和延迟有可观测数据证明收益。

## 17. 测试策略

### 17.1 单元测试

覆盖：

- `scripts-list.md` runtime 占位符解析。
- public `script_catalog` 过滤 runtime 参数。
- `<runtime.session_id>` 正确填充。
- 普通 `<session_id>` 不触发 runtime 注入。
- 用户 `arguments.session_id` 不能覆盖 runtime session_id。
- 缺失业务参数返回 `missing_args`。
- 缺失 runtime 参数返回系统错误。

### 17.2 集成测试

覆盖：

- 配置关闭时，请求仍进入 Agent-loop。
- 配置开启且 selected ontology 明确时，命中脚本 direct 执行。
- direct branch 输出完整 SSE 序列。
- 脚本执行失败时 run/turn 状态正确完成或失败。
- direct route fallback 后，Agent-loop 行为与原来一致。

### 17.3 回归测试

重点保护：

- `test_tool_registry` 中现有 `execute_script` 行为。
- `test_agent_loop` 中 Agent-loop 调用 `execute_script` 的行为。
- `test_routes_stream` 中 SSE 协议。
- `test_orchestrator_runtime` 中 session/run 生命周期。

## 18. 主要风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| operation 误匹配 | 错误执行脚本 | 第一阶段只做高置信度确定性匹配，不确定 fallback |
| runtime 参数污染业务参数 | 用户可伪造 session | 使用 `<runtime.*>` 专属占位符，禁止用户覆盖 |
| 旧脚本收到新增参数 | 旧流程失败 | 只有显式声明 runtime 占位符才注入 |
| direct 分支破坏 session 记忆 | 多轮上下文异常 | direct branch 仍走 orchestrator run/session/turn 持久化 |
| 脚本副作用重复执行 | 数据不一致 | 脚本开始执行后不自动 fallback 重试 |
| 前端无法展示 direct 结果 | 用户体验割裂 | 沿用 `agent-stream.v2` 和 `execute_script` tool events |

## 19. 推荐决策

建议采用本方案，并按阶段一先落地：

1. direct branch 放在 `AgenticOrchestrator.run()` 中，位于 `turn.started` 之后、Jiuwen ReAct loop 之前。
2. direct branch 默认关闭，通过配置灰度启用。
3. 第一阶段只支持 selected ontology 下的脚本型 operation。
4. `session_id` 通过 `<runtime.session_id>` 显式占位符注入。
5. 旧脚本和旧 `scripts-list.md` 不受影响。
6. direct branch 和 Agent-loop 共用 `execute_script`，不新增第二套脚本执行器。
7. 不确定就 fallback，不为了命中率牺牲主链路稳定性。

这个方案的核心价值是：把“明确脚本型 operation 的快速执行”变成一条独立、可关闭、可回退、可观测的分支，同时保留现有 Agent-loop 的完整能力。

## 20. 评审关注点

建议技术评审重点确认以下问题：

1. direct branch 的插入点是否应在 `turn.started` 之后。
2. 第一阶段是否只支持 selected ontology。
3. `<runtime.session_id>` 是否足够表达脚本对会话上下文的依赖。
4. public `script_catalog` 是否应该完全隐藏 runtime 参数。
5. 脚本执行失败时 `turn.finished.status` 应使用 `success` 还是 `error`。
6. 是否需要把 direct branch 的执行结果写入 LLM context，供后续多轮 Agent-loop 参考。
7. 是否需要为 runtime 参数建立允许列表，禁止任意 `<runtime.xxx>` 扩展。
