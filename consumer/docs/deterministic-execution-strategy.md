---
scope: deterministic-execution-v2
module: agent-runtime
feature_type: functional
feature_id: FEAT-DET-001
status: active
updated: 2026-08-17
---

# 确定性执行策略

## 1. 特性定位

FEAT-DET-001 定义当前版本的确定性 Operation 执行事实：知识包中的 Operation 只有显式声明 `execution.type: deterministic`，并在导入阶段完成严格校验和锁定后，才可以在 Agent Loop 之前进入确定性执行分支。该分支负责受控路由、结构化输入、可选 Parser、单次 Handler 执行、只读数据快照绑定、沙箱隔离、权威结果封装以及终态事务提交。

本特性解决的问题是：对于已经具备明确输入、固定算法和可验证输出契约的 Operation，运行时不应继续依赖模型选择工具、补全参数或解释结果。相同的知识包版本、结构化参数、运行时修订和数据修订应形成可审计的结果证据；任何包内容、运行时、数据作用域或输出契约变化都必须使当前执行失败关闭，而不是静默退回模型执行。

当前版本保留 Agent Loop 作为兼容主路径。未声明确定性执行、未选择知识包或未命中确定性 Operation 的请求返回 `NotApplicable`，随后进入既有直接执行或 Agent Loop。只要请求已经选中确定性 Operation，功能关闭、锁缺失、输入不合法、Provider 不合格、沙箱不可用、输出不合规或持久化失败都必须形成确定性失败，不得降级到另一条执行路径。

本特性面向以下角色：

- 知识包开发者：通过 Operation Front Matter、Schema、Parser 和 Handler 描述确定性契约。
- 知识包发布与导入方：在安装版本前完成静态校验并生成包锁。
- Agent Runtime：在模型解析之前识别确定性请求，执行并提交权威结果。
- 数据源运维方：为允许的数据 Provider 声明只读、不可变且隔离的数据修订。
- 部署运维方：构建应用与沙箱运行时修订，并提供可验证的共享证明。
- 协议与前端集成方：透传并展示同一结果信封，不重新计算业务结果。
- 测试与验收团队：按本特性定义的成功、等待、失败、取消和恢复语义进行验证。

## 2. 当前版本能力要求

| 能力 | 要求级别 | 事实要求 |
|---|---|---|
| 显式 Operation opt-in | MUST | 只有 Front Matter 中存在 `execution` 映射且 `execution.type` 严格等于 `deterministic` 的 Operation 才能进入本分支。旧知识包和未声明 Operation 保持原有行为。 |
| 默认关闭 | MUST | `DETERMINISTIC_EXECUTION_ENABLED` 默认必须为 `false`。命中确定性 Operation 但功能未启用时返回稳定失败，不得继续 Agent Loop。 |
| 模型配置延迟解析 | MUST | 只读预检判断请求可能命中确定性 Operation 时，运行时必须延迟 LLM Profile 解析；确定性成功、等待或失败不得依赖可用的模型配置。 |
| 兼容回落 | MUST | 只有 `NotApplicable` 可以继续既有直接执行或 Agent Loop。`AwaitingInput`、`Completed` 和 `Failed` 都是本分支的权威结果。 |
| 确定性路由 | MUST | 显式 Operation key、规范化别名或受限命令形态可以选择 Operation；匹配必须使用 Unicode NFKC 规范化。多 Operation 命中必须返回歧义失败，不得猜测。 |
| 包发布校验 | MUST | 导入或发布阶段必须校验 Operation、Schema、Parser、Handler、脚本描述文件和 Python 本地依赖闭包。无确定性声明的包不得被新规则误伤。 |
| 包锁 | MUST | 校验成功后必须原子生成 `.deterministic-lock.json`，记录锁版本、包修订、完整文件清单、文件大小与摘要，以及 Operation 路由索引和执行绑定。 |
| 完整性双检 | MUST | Handler 执行前和输出验收前必须验证完整包锁。修改、删除或新增任何影响包闭包的文件都必须使执行失败关闭。 |
| 已安装版本不可变 | MUST | 已锁定的确定性知识包版本不得通过文件编辑、Operation 编辑、在线演进或同版本覆盖安装进行原地修改。演进必须发布新版本并重新生成锁。 |
| Schema 契约 | MUST | 输入和输出必须使用可离线解析的 JSON Schema Draft 2020-12 对象根。远程 `$ref`、远程动态引用、嵌套 `$id`、重复 JSON key 和非标准 JSON 常量必须被拒绝。 |
| 可选 Parser | MAY | Operation 可以声明独立 Python Parser，把自然语言或显式参数转为结构化业务参数。Parser 必须位于 `scripts/`，且不能与 Handler 共用同一文件。 |
| 参数优先级 | MUST | 恢复检查点参数、Parser 参数和当前请求显式参数按此顺序合并，当前显式参数优先。Parser 不得重新打开已确认问题或覆盖运行时拥有字段。 |
| 运行时字段隔离 | MUST | session、run、turn、user、workspace、ontology 等运行时拥有参数只能由 Runtime 注入，不得由请求、Schema、Parser 或检查点伪造。 |
| 缺参交互 | MUST | 缺少必填字段且当前协议支持交互时，必须返回 `AwaitingInput`，并持久化绑定 Operation、包修订、运行时修订和 Provider 修订的检查点。 |
| 非交互缺参 | MUST | 当前入口不支持用户交互时，缺少必填字段必须返回稳定失败，不得通过模型补全或以空值执行。 |
| 单次 Handler 执行 | MUST | 一个成功结果的 Handler 必须执行一次，权威信封中的 `handler_execution_count` 固定为 `1`。Parser 与 Handler 使用独立执行身份。 |
| 沙箱强制隔离 | MUST | Parser 和 Handler 必须在受支持沙箱中执行。确定性分支不得使用本地进程回退。沙箱不可用、释放失败或证明不匹配都必须失败关闭。 |
| 运行时修订证明 | MUST | 应用运行时修订必须绑定行为相关源码、解释器与安装发行包；沙箱修订必须绑定运行契约、架构、依赖和 Runner 代码。执行前后必须验证修订未变化。 |
| 固定执行环境 | MUST | Runtime 必须注入固定环境、只读知识包、受限网络端点、超时、输出上限和独立产物目录；Handler 不得获得未声明的环境能力。 |
| 受限 Python 闭包 | MUST | 构建期必须拒绝随机数、当前时间、动态导入、子进程、进程派生和其他不允许的非确定性 API；外部依赖和 Runtime SDK 导入必须位于部署契约允许列表。 |
| Provider 修订绑定 | MUST | `data_access: workspace_provider` 只允许当前支持的只读图数据 Provider，并要求数据修订、访问范围修订、不可变快照声明、隔离声明和完整物理选择器。 |
| Provider 凭据保护 | MUST | 凭据必须位于独立认证字段，禁止嵌入 endpoint、query 或 fragment。凭据不得进入作用域摘要、结果、产物、公开错误或日志。 |
| Provider 网络收敛 | MUST | Handler 只能访问本次已绑定 Provider 的有效网络端点。模型型 Provider、未固定数据修订或可变快照不得用于确定性执行。 |
| 严格 Handler 输出 | MUST | Handler 标准输出必须是单个标准 JSON 对象，不得包含重复 key、NaN 或 Infinity，并且必须满足输出 Schema。 |
| 有界标准输出 | MUST | Parser stdout 上限为 64 KiB，Handler stdout 上限为 5 MiB，脚本 stderr 上限为 64 KiB。超限必须终止或拒绝执行。 |
| 有界产物 | MUST | 当前版本最多接受 32 个产物；单文件不超过 10 MiB，总大小不超过 20 MiB，产物树不超过 128 个条目。路径、媒体类型、NFKC 名称唯一性、摘要和敏感值必须全部通过检查。 |
| 产物冻结与回滚 | MUST | Handler 沙箱及其后代进程终止后才能冻结并检查产物。任一产物写入、摘要校验或终态提交失败时，已经持久化的同批产物必须回滚或由恢复日志协调。 |
| 权威结果信封 | MUST | 成功结果必须包含结构化结果、产物引用、Operation 身份、包修订、Handler 摘要、运行时修订、依赖修订、参数摘要、结果摘要、耗时和单次执行计数，并标记 `authoritative: true`。 |
| 终态事务 | MUST | 成功、等待和失败必须在串行事务内协调事件日志、会话上下文、检查点、产物日志和 RunStore 状态；进程异常后必须能够恢复待提交终态。 |
| 取消优先 | MUST | 取消信号必须传递给包校验、Parser、Handler 和产物监控。已确认的 `CANCELLED` 终态不得被迟到的成功或失败覆盖。 |
| 协议结果一致性 | MUST | HTTP、A2A、ACP、MCP 与前端必须消费同一编排事件和最终结果信封，保留结构化结果、产物引用和稳定错误码，不得在协议层重新执行业务计算。 |
| 自动推断旧包语义 | OUT | 当前版本不根据脚本内容或 Operation 名称自动把旧知识包升级为确定性包。 |
| 任意本地脚本回退 | OUT | 当前版本不允许在沙箱失败时改用 Backend 本地进程执行 Parser 或 Handler。 |
| 任意外部数据源 | OUT | 当前版本不承诺对未支持 Provider、可变查询结果或无物理快照选择器的数据源提供确定性保证。 |

## 3. 外部接口与入口要求

| 入口或契约 | 类型 | 事实要求 |
|---|---|---|
| Operation `execution` Front Matter | 知识包 YAML | 必须声明 `type: deterministic`、`input_schema` 和 `output_schema`；`parser` 可选。未知字段必须拒绝。 |
| Operation key 与 aliases | 知识包 YAML | 作为确定性路由 token；与其他 Operation 冲突、NFKC 归一化后重复或一个 key 多处声明时必须拒绝发布。 |
| Handler descriptor | 知识包 Markdown/YAML | 必须唯一绑定 Operation，提供 Python entrypoint；`data_access` 只能是 `none` 或 `workspace_provider`。 |
| `.deterministic-lock.json` | 安装产物 | 是运行时读取的包完整性索引。当前实现写入锁版本 2，并兼容读取版本 1；缺失、损坏或与声明不一致时必须失败关闭。 |
| `DETERMINISTIC_EXECUTION_ENABLED` | Runtime 配置 | 控制确定性 Handler 是否允许执行，默认关闭。该开关不改变包保护、路由识别或失败关闭语义。 |
| `DETERMINISTIC_ARTIFACT_STAGING_ROOT` | Runtime 配置 | 可选的共享绝对目录，用于 Backend 与沙箱之间的产物暂存。相对路径、不可创建目录或租约刷新失败必须拒绝执行。 |
| 沙箱运行契约 | JSON + requirements | 固定 Runner 协议、Python 入口、允许的外部依赖和公开 Runtime SDK 导出，并参与沙箱运行时修订计算。 |
| 共享沙箱证明 | 只读文件 | 发布部署中由沙箱镜像写入，Backend 必须读取并与期望修订比较；缺失、格式错误或不匹配时不得执行。 |
| 显式 Operation 请求 | Runtime context / arguments | 调用方可以通过上下文中的 Operation key 和结构化 arguments 请求执行。显式确定性 key 的预检不得依赖 LLM 校验。 |
| 用户交互响应 | Runtime interaction | 必须携带与待处理检查点一致的 interaction id 和结构化值；过期、Operation 改变或修订改变时返回 `stale_checkpoint`。 |
| 成功结果信封 | JSON object | 顶层包含 `status`、`text`、`structured_result`、`artifacts` 和 `execution`。`execution` 是证明和审计字段，不是重新执行指令。 |
| 失败结果信封 | JSON object | 包含稳定错误 code、公开 message、`retryable: false` 以及非权威、未完成的 execution 标记；内部异常细节不得穿透协议边界。 |
| 现有协议入口 | HTTP / A2A / ACP / MCP | 本特性不新增独立 northbound endpoint。所有入口必须通过同一 Orchestrator 分支获得确定性语义。 |

Operation 最小声明示例：

```yaml
---
key: calculate_summary
aliases:
  - 汇总计算
execution:
  type: deterministic
  parser: scripts/parse_summary.py
  input_schema: schemas/summary.input.json
  output_schema: schemas/summary.output.json
---
```

示例仅表达契约形态；实际 Handler 仍必须通过独立脚本描述文件与 `calculate_summary` 唯一绑定。

## 4. 场景与用户旅程

| 场景 | 前置条件 | 用户/系统动作 | 期望行为 |
|---|---|---|---|
| 旧知识包请求 | 已选择的包没有确定性 Operation | 用户发起普通请求 | 预检返回不适用，不进行完整包哈希，不阻塞既有直接执行或 Agent Loop。 |
| 显式确定性执行 | 包已锁定、功能启用、参数完整 | 调用方提供 Operation key 和 JSON arguments | Runtime 不解析模型配置，验证包和运行时后在沙箱执行一次 Handler，提交权威结果信封。 |
| 别名命令执行 | 请求以唯一 Operation 别名和明确参数形态发起 | Runtime 执行受限匹配 | 唯一命中时进入确定性分支；只是解释、否定或模糊提及 Operation 时不得执行。 |
| 功能关闭 | 请求明确命中确定性 Operation，但开关关闭 | Runtime 完成包识别 | 返回 `feature_disabled`，不启动 Handler，也不回落到模型。 |
| 输入不足且支持交互 | Schema 缺少必填字段 | Runtime 请求用户补充 | Run 进入等待状态，持久化检查点；恢复时重新验证包、运行时和 Provider 修订，Handler 只执行一次。 |
| 输入不足且不支持交互 | 当前协议不支持继续交互 | Runtime 校验输入 | 返回稳定缺参失败，不调用 Parser 以外的补全机制，不进入 Agent Loop。 |
| Parser 解析 | Operation 声明独立 Parser | Runtime 将请求和显式参数传入 Parser | Parser 在独立沙箱中返回结构化参数或待确认问题；显式参数优先，运行时字段不可覆盖。 |
| 只读快照查询 | Handler 声明 `workspace_provider` 且数据源满足确定性约束 | Runtime 绑定当前 workspace、知识包版本、物理选择器和数据修订 | Handler 只能访问已绑定端点；结果信封记录无凭据的依赖修订和作用域摘要。 |
| 包执行前被修改 | 锁文件或任一包文件与安装时不一致 | Runtime 验证完整包 | 在 Parser/Handler 启动前返回完整性失败。 |
| Handler 执行中包或运行时变化 | Handler 已启动，随后包或运行时证明变化 | Runtime 执行后复检 | 当前输出和产物全部失效，返回失败且不形成权威成功。 |
| 产物超限或含受保护值 | Handler 写入不合规产物 | 产物监控检测到策略违反 | 取消 Handler、释放沙箱、冻结目录并回滚同批产物，返回稳定策略失败。 |
| 用户取消 | Run 正在校验、解析或执行 | 用户发出取消请求 | Runtime 终止沙箱并提交取消事件；迟到输出不得把 Run 改成成功或失败。 |
| 终态提交中断 | 事件或终态日志准备完成，但进程在提交边界异常 | 下一次会话读取或运行触发恢复 | Runtime 根据待提交日志去重、提交或回滚，避免成功状态、消息、事件和产物不一致。 |

## 5. 行为语义与边界

### 5.1 核心行为语义

#### 5.1.1 选择与回落语义

- Runtime 在 LLM Profile 解析前进行只读预检。显式 key、检查点或可能命中的命令形态可以使模型解析延迟。
- 预检只用于控制路由成本，不能代替执行阶段的完整锁校验。
- `NotApplicable` 表示本特性没有取得请求所有权，允许继续其他执行路径。
- 一旦选中确定性 Operation，本分支取得请求所有权。后续任何错误均形成确定性失败，不允许 Agent Loop 修复、润色或重算。
- 同一请求只能选择一个确定性 Operation。歧义、冲突或已锁路由索引变化必须失败关闭。

#### 5.1.2 包契约与完整性语义

- 包锁覆盖完整版本文件集合，而不只是 Handler 文件。新增可导入 Python 模块同样会改变行为，必须被完整性校验发现。
- 锁版本 2 把 Operation path、规范化路由 token、Handler、Parser 和 Schema 绑定到安装时声明。版本 1 兼容读取时必须在路由前先验证关键声明文件。
- Python 本地导入闭包必须解析到包内；路径解析不得通过 `..`、绝对路径、符号链接或 namespace 逃逸包根。
- 锁文件是完整性和行为修订证据，不是数字签名、来源授权或发布审批。
- 包安装完成后，确定性 Operation、绑定 Handler 和相关上下文必须受到编辑与在线演进保护。

#### 5.1.3 输入、Parser 与检查点语义

- 输入必须先转换为标准 JSON 值，再按 Draft 2020-12 Schema 校验。非标准数字和重复 key 不得进入 Parser 或 Handler。
- Parser 只负责形成业务参数或可交互问题，不拥有运行时上下文，也不能放宽 Schema。
- 参数合并顺序固定为：检查点参数 < Parser 参数 < 当前显式参数。
- 缺参检查点必须绑定 ontology id/version、Operation key、包修订、运行时修订、Provider 依赖修订、已确认参数和待确认问题。
- 恢复执行前必须重新校验所有绑定。任一绑定变化都使检查点过期，且不得静默创建新执行。

#### 5.1.4 Provider 与凭据语义

- 当前确定性数据访问只接受实现中明确支持的图数据 Provider；数据源必须同时声明只读、不可变快照和 workspace/ontology 隔离。
- `snapshot_selector` 必须等于实际数据库或完整图目标，不能只使用审计标签代替物理选择器。
- `scope_digest` 绑定 Provider、endpoint、认证类型、非秘密主体标识、选项、数据修订和访问范围修订，但不包含 password 或 token；单纯凭据轮换不应改变业务作用域摘要。
- Provider endpoint 禁止 user-info、敏感 query 参数和 fragment。发现旧格式凭据时必须拒绝并在所有公开输出中脱敏。
- Handler 只获得本次有效连接信封和单一网络端点；连接秘密不得进入结果摘要或产物。

#### 5.1.5 沙箱与运行时修订语义

- 应用运行时修订绑定 Backend 行为文件、Python 实现和安装发行包；沙箱修订绑定运行契约、依赖、架构和执行代码。
- 发布部署必须通过只读共享证明把沙箱镜像修订提供给 Backend。环境变量只作为受控开发后备，不能覆盖有效的构建或共享证明。
- Parser 和 Handler 使用隔离执行身份；取消或严格释放失败时，相关沙箱必须保留为失败事实，不能把未完全终止的输出视为权威。
- Handler 完成后必须先销毁运行沙箱及其后代进程，再冻结和检查 stdout 与产物，避免后台进程继续修改结果。
- 执行前记录的运行时修订必须在 Handler 后再次读取并完全一致。

#### 5.1.6 输出、产物与权威结果语义

- Handler stdout 是唯一结构化主结果，必须解析为标准 JSON 对象并满足输出 Schema。
- 产物是主结果的有界附属输出，只能通过 Runtime 提供的暂存目录产生。Runtime 必须限制数量、大小、路径、树条目数、媒体类型和名称唯一性。
- 写入持久化存储后的 `output_ref`、size 和 sha256 必须与冻结产物一致。存储层擅自改变引用或摘要必须视为失败。
- 成功信封的 `arguments_sha256` 只覆盖业务参数，不包含运行时拥有参数或秘密；`result_sha256` 覆盖结构化结果与产物的稳定元数据。
- 前端可以从 `text`、`formatted_result`、`message` 或结构化 JSON 生成展示内容，但不得改变 `structured_result` 和 `execution` 事实。

#### 5.1.7 终态、恢复与取消语义

- 确定性成功、等待和失败必须通过 RunStore 事务与 ExecutionStore 小型恢复日志协调提交。
- 事件写入必须具备去重恢复能力。事件已经落盘但终态提交未完成时，后续恢复不能重复产生逻辑事件。
- 产物先预留、后写入、再随终态提交。终态失败或取消时必须回滚；终态已经权威提交但产物日志提交延迟时，恢复逻辑只能协调日志，不得删除成功结果引用的产物。
- `COMPLETED`、`FAILED`、`CANCELLED` 是不可改写终态。取消与迟到结果竞态时，以已经持久化的取消终态为准。
- 当前恢复能力处理待提交终态和产物日志，不承诺从进程退出前的 Python 指令位置继续 Handler。

#### 5.1.8 错误、状态与可观测结果

| 场景 | 事实要求 |
|---|---|
| 请求未选择确定性能力 | 返回 `NotApplicable`，不作为错误记录。 |
| 功能未启用 | 返回 `feature_disabled`，Handler 执行次数为 0。 |
| Operation 不存在或歧义 | 返回 `operation_not_found`、`operation_ambiguous` 或对应稳定包错误。 |
| 包锁缺失、无效或变化 | 返回稳定完整性错误，不泄露内部文件正文。 |
| 输入或 Schema 不合法 | 返回 `invalid_arguments`、`execution_schema_invalid` 或 `input_schema_violation`。 |
| 缺少可交互输入 | 返回 `AwaitingInput`；非交互入口返回 `missing_arguments`。 |
| Provider 不合格 | 返回稳定 Provider 错误；不得包含 endpoint 凭据、用户名或连接信封。 |
| 沙箱或运行时证明失败 | 返回 `deterministic_isolation_unavailable` 或运行时修订错误，不允许本地回退。 |
| Handler 超时、取消或输出过大 | 终止执行并返回稳定错误码；`handler_execution_count` 反映 Handler 是否已启动。 |
| Handler JSON 或输出 Schema 失败 | 返回 `invalid_json_output` 或 `output_schema_violation`，不持久化权威成功。 |
| 产物策略或持久化失败 | 返回 `artifact_policy_violation` 或隔离错误，并回滚同批引用。 |
| Provider 秘密出现在结果或产物 | 返回 `secret_output_violation`，公开错误中不得复述秘密。 |
| 用户取消 | 形成 `CANCELLED` 终态和取消原因，后续完成事件不得覆盖。 |
| 终态提交异常 | 保留待提交日志并在后续安全点恢复；无法确认持久化结果时不得报告新的成功。 |

### 5.2 显式边界与不承诺项

| 边界 | 当前版本不承诺 |
|---|---|
| 旧知识包自动升级 | 不从脚本、文档或历史行为推断确定性语义；必须显式声明并重新发布。 |
| 模型参与确定性计算 | 不使用 LLM 选择已锁 Operation、补全必填参数、解释输出或校验业务结果。 |
| 任意 Python 能力 | 不允许随机数、当前时间、动态导入、子进程、进程派生或未在运行契约中声明的第三方依赖。 |
| 任意网络访问 | 不允许 Handler 自由访问网络；只有已绑定、通过校验的 Provider endpoint 可以进入沙箱策略。 |
| 任意 Provider | 不为模型 Provider、可变数据源、无 revision 数据源或未隔离的共享数据面提供确定性保证。 |
| 写数据事务 | 当前 Provider 契约只面向只读快照，不承诺外部数据库写入的确定性、幂等或回滚。 |
| 包锁即签名 | `.deterministic-lock.json` 不证明作者身份、代码来源、审批状态或供应链可信度。 |
| 跨崩溃 exactly-once | `handler_execution_count: 1` 证明当前成功信封对应一次 Handler 执行，不承诺跨进程崩溃、人工重试或外部副作用的端到端 exactly-once。 |
| Handler 中途续行 | 当前检查点只覆盖执行前的用户输入等待；不保存 Python 栈或从 Handler 中间位置恢复。 |
| 协议私有结果 | 不为 HTTP、A2A、ACP 或 MCP 定义不同的业务结果结构；协议只做兼容映射。 |
| 同版本在线变更 | 不允许已锁定版本原地编辑或自动演进；必须产生新版本与新锁。 |
| 无界产物与二进制输出 | 不接受任意数量、任意大小、任意路径或未允许媒体类型的产物。 |

## 6. 对下游设计与实现的约束

- 知识包导入与新版本安装必须调用确定性 Operation 校验和锁生成；不得通过复制预制锁跳过当前代码校验。
- Operation、脚本编辑器和在线演进服务必须查询锁定状态，并拒绝修改受保护的 Operation、Handler 或上下文。
- Orchestrator 必须在 LLM 配置解析前完成只读预检，并坚持“只有 `NotApplicable` 才允许回落”的分支规则。
- Parser、Handler、Provider、Schema、包锁或运行时证明新增任何能力时，必须同步更新包修订或运行时修订材料。
- 沙箱镜像构建必须使用版本化运行契约和固定依赖生成证明；Backend 与沙箱必须共享同一只读证明表面。
- Provider 配置必须把秘密放在专用认证字段，并提供数据修订、访问范围修订、不可变快照、隔离声明和完整物理选择器。
- 协议适配层必须保留 `turn.finished` 的结构化 final output、稳定错误码、等待交互和取消状态；不得只保留展示文本。
- 前端必须把确定性信封视为权威结果，展示结构化结果与有效产物引用，不调用模型二次润色。
- 测试必须覆盖：包锁构建与篡改、路由冲突、Schema/Parser/Handler 契约、运行时字段隔离、缺参恢复、Provider 修订变化、凭据脱敏、沙箱证明、单次执行、超时与取消、输出及产物限制、终态与产物恢复、协议透传和旧 Agent Loop 回归。
- 新增 Provider、产物类型、网络能力、恢复语义或成功信封字段前，必须先更新本特性文档及相应安全边界，再进入实现。

## 7. 关联实现与验证

- `consumer/backend/ontology/operation_execution.py`：确定性包声明、静态校验、锁生成与完整性验证。
- `consumer/backend/agent/direct_execution/service.py`：路由、Parser、输入校验、Handler、Provider、产物和结果信封执行主链。
- `consumer/backend/agent/direct_execution/audit.py`：应用/沙箱运行时修订与权威结果信封。
- `consumer/backend/agent/core/orchestrator.py`：预检、模型解析延迟、结果事件、终态事务、取消和回落。
- `consumer/backend/persistence/deterministic_terminal.py`：待提交终态和产物日志恢复。
- `consumer/backend/integration/sandbox/runtime-contract.json`：沙箱 Runner、依赖与公开 SDK 契约。
- `consumer/backend/onto_sandbox_runtime/bounded_runner.py`：stdout、stderr、文件大小和进程生命周期限制。
- `consumer/backend/tests/test_deterministic_package.py`：包契约、锁、不可变性和静态安全验证。
- `consumer/backend/tests/test_deterministic_execution_service.py`：执行、交互、Provider、沙箱、输出和产物验证。
- `consumer/backend/tests/test_deterministic_provider_revision.py`：数据修订、物理作用域和凭据隔离验证。
- `consumer/backend/tests/test_deterministic_runtime_revision.py`：应用与沙箱运行时证明验证。
