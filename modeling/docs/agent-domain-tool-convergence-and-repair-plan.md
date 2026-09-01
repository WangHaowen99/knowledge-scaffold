# Agent 领域工具收敛与候选修复闭环方案

## 状态

本文是针对真实长流程中暴露出的工具选择、候选修复和前端状态表达问题形成的设计及实现记录。

方案最初在独立分支 `fix/modeling-domain-tool-convergence` 实现，现已回合到
`feat/modeling-domain-patch-v2`。回合时同时保留了该分支的对象来源策略：Schema 首次生成走确定性全量同步，
专家增量重绑走单对象 Patch，无 Schema 时只允许虚拟对象。本次回合没有部署或覆盖 cs 机器的 5100 服务。

实现采用两套有明确边界的工具配置：新运行使用 `domain-v2`，历史 checkpoint 数据迁移为 `legacy-v1`。
前者收敛到新领域工具和 Change Set，后者暂时保留创建 checkpoint 时已有的工具 schema，避免 OpenJiuwen SDK
恢复历史会话时发生 schema 不一致。无论使用哪一套配置，Core 的受管路径门禁、原子绑定和完整校验都不会放宽。

相关背景：

- [Agent 受控修改、计划生成与浏览器验收方案](agent-controlled-change-set-problems-and-browser-e2e.md)
- [Agent 领域增量修改与受管文件决策](decisions/2026-08-30-agent-domain-mutation-v2.md)
- [无损候选修复与领域工具](decisions/2026-08-27-lossless-candidate-repair-and-domain-tools.md)

## 1. 问题概述

一次知识生成运行中，Agent 已经生成部分对象、操作、规则和指标，但随后发生以下过程：

1. Agent 使用自由文件工具写领域文件，快速格式校验拒绝了不一致内容；
2. Agent 修正了部分文件，但继续尝试直接写受管对象映射 JSON；
3. Core 正确拒绝直接写入，并明确返回应使用对象绑定领域工具；
4. Agent 没有调用已经注册的绑定工具，反而判断为“当前环境无法补写”；
5. 不同种类的失败共同消耗候选自动修正额度；
6. 达到修正上限后，Agent 把可由系统解决的技术问题升级成用户业务决定；
7. 前端把历史已修正错误、当前错误和校验摘要都显示为独立红色消息，用户无法判断真实状态。

改造前的机制做对了两件事：错误映射没有进入候选或正式知识包，重复生成在达到上限后停止。
缺失的闭环是：选错工具后缺少确定性转向、重复工具仍同时暴露、修正预算粒度过粗，前端也没有表达问题生命周期。

## 2. 第一性原理

### 2.1 模型不应决定系统能力

模型可以判断用户意图和业务语义，但不能靠自然语言猜测“平台能不能做”。工具是否注册、当前阶段是否允许、
参数是否完整，必须由 Core 和 Agent Rail 确定性判断。

### 2.2 一个业务事实只能有一个权威来源

对象来源表、字段映射和主键属于同一个业务事实。不能允许对象 Markdown 和映射 JSON 分别维护，
否则任一中途失败都会产生两边不一致。

### 2.3 技术错误不能变成业务问题

选错工具、阶段不匹配、参数格式错误和版本冲突都是技术恢复问题。只有字段对应关系存在真实业务歧义，
或者资料没有给出必要口径时，才能询问用户。

### 2.4 用户只需要看到当前有效状态

工具失败可以进入审计记录，但主对话不应永久堆叠已经解决的错误。用户需要区分：

- 已阻止且没有写入；
- 系统正在自动修正；
- 已修复；
- 当前仍需用户处理。

## 3. 改造目标

1. 新运行不能通过自由文件工具修改受管领域资源；
2. 老 checkpoint 中的旧工具调用能够安全迁移，不会直接中断；
3. 对象来源和映射始终在一个 Core 事务中更新；
4. 只有重复出现的同类 finding 才消耗同一修正预算；
5. 已存在恢复工具时，Agent 不得宣称平台无法处理；
6. 前端只突出当前阻塞，历史错误能够显示最终处理结果；
7. 所有改造同时作用于专家增量修改和正常知识生成流程。

## 4. 六层修复方案

### 4.1 第一层：收紧 Agent 工具入口

新运行进入知识建模或增量建模阶段后，模型只看到计划编排和受控例外工具，不看到领域文件的自由写入路径。

模型侧的主要写入口是：

```text
stage_change_set
sync_schema_objects
patch_object_source_binding
preview_domain_delete / delete_domain_resource
define_script_contract / stage_script_source / run_script_test
```

`create_*` 和 `patch_*` 继续作为 `stage_change_set` 的结构化 action 和 Core handler 存在，但不注册到新运行在
知识建模、增量建模阶段的模型 ToolCard。`write_file` 和 `delete_file` 在这两个阶段也不注册给新运行；其他阶段即使
能够使用通用文件工具，也只能处理以下非受管内容：

- `.modeling/` 中允许模型维护的分析和工作文件；
- `diagrams/` 中经过流程图校验的源码；
- 平台明确声明为自由文件的其他扩展内容。

对象、规则、操作、指标、脚本、索引、映射和内部语义规格不得进入自由文件工具 schema。
Core 仍永久保留路径门禁，防止旧客户端或未来代码绕过 Agent 工具过滤。

### 4.2 第二层：为旧流程增加确定性工具转向

这一层主要服务于部署前已经保存的 checkpoint、未完成运行和旧客户端，也作为新流程的纵深防御。

Core 拒绝旧调用时返回结构化恢复动作，而不只返回一段提示文字：

```json
{
  "error": "MANAGED_FILE_REQUIRES_DOMAIN_TOOL",
  "requiredAction": "redirect_tool",
  "requiredTool": "sync_schema_objects",
  "targetKind": "schema_objects",
  "issueId": "stable-issue-id",
  "retryable": false
}
```

Agent Rail 收到该结果后直接进入一个有界恢复流程：

1. 读取当前 Change Plan、对象来源模式和资源快照；
2. Schema 生成计划调用 `sync_schema_objects`，由 Core 按全部 Sheet 生成参数；
3. 专家重绑计划读取目标对象、真实数据表及字段，再调用 `patch_object_source_binding`；
4. 无 Schema 的虚拟对象模式直接拒绝物理 mapping，不生成不可执行的恢复动作；
5. 重新执行完整候选校验；
6. 校验通过后恢复原计划，不创建新的业务问题。

实现使用 `tool-redirect-workflow-v1` OpenJiuwen Workflow，把持久化的 `issueId`、`requiredTool`、项目阶段和
候选修订转换成有界恢复指令，并使用独立 session 保存恢复检查点。真正的修改仍通过 Agent Tool 和 Core 完成，
Workflow 本身不直接写知识包。模型只处理字段语义匹配，字段存在有证据的真实歧义时才允许生成用户决定卡。

### 4.3 第三层：统一对象来源事实并原子渲染

内部结构化对象规格是对象来源的唯一事实源，包含：

- 来源资料 ID；
- 来源表；
- 对象字段到数据列的完整映射；
- 主键；
- 证据；
- 工作修订和资源 Hash。

对象 Markdown、对象索引、`mappings/object-data-mappings.json` 和内部语义规格都由 Core 从该事实渲染，
不得被 Agent 分别写入。

对象来源按互斥模式处理：

- 存在 `purpose=schema` 的已解析表格且执行首次生成时，`sync_schema_objects` 按每张 Sheet 恰好一个对象同步；
- 存在 Schema 且执行已批准的专家重绑时，`patch_object_source_binding` 只修改计划指定的一个对象；
- 完全没有 Schema 时，`create_object` 只能生成不绑定物理表的虚拟对象，不生成 mapping；
- 模型不得合并、遗漏 Schema Sheet，也不得在 Schema 模式追加派生对象。

两个对象来源工具都必须在同一 Core 事务中完成：

1. 校验计划任务、工作修订和对象 Hash；
2. 校验资料、表、字段和主键真实存在；
3. 更新结构化对象事实；
4. 重新渲染对象和映射派生文件；
5. 运行交叉一致性校验；
6. 保存幂等回执并形成一个原子候选组。

Schema 首次生成由 Core 把模型提交的对象任务正规化成一个 `sync_schema_objects` 计划任务，字段、主键、来源、
对象 Markdown、对象索引、映射 JSON、内部语义规格和幂等回执一次生成。专家重绑仍使用带 `plan_task_id`、
资源 Hash 和幂等 ID 的 `patch_object_source_binding`。任一校验或渲染失败都整批回滚。

这两条路径共同补齐旧 `upsert_object(source)` 的能力，因此新运行可以隐藏 `upsert_object`，同时避免模型在
Schema 模式自由决定对象数量和字段。

### 4.4 第四层：按问题身份管理修正预算

当前候选级统一 `3` 次预算会把格式错误、工具误用和阶段错误混在一起。新状态至少包含：

```text
candidateRevision
validatorVersion
errorClass
findingFingerprint
issueAttempts
repairRoundId
attempt
progressReceipt
status
```

计数规则调整为：

- 选错工具并成功确定性转向：不计入候选修正次数；
- 阶段不匹配：记录依赖等待，不计入格式修正次数；
- 版本冲突：重新读取后计算新资源 Hash，不计入内容修正次数；
- finding 指纹发生变化且有确定性进展回执：开始新的问题轮次；
- 修改后相同 finding 指纹再次出现：同一问题 `attempt + 1`；
- 相同问题达到上限：只停止该问题，不冻结无关候选修复。

通用“从当前进展继续”不能直接清空停止状态。只有以下确定性动作可以开启新轮次：

- 已执行要求的恢复工具；
- 用户修改了业务约束；
- 用户撤销了对应候选；
- 候选或校验器版本已经变化。

### 4.5 第五层：增加能力声明和业务提问门禁

Agent 准备发送以下内容前，Rail 必须检查真实工具注册表和阶段权限：

- “当前环境无法处理”；
- “需要平台人工补写”；
- “只能等待管理员”；
- 技术失败对应的业务决定卡。

如果错误中带有 `requiredTool`，并且该工具当前可用，Rail 拒绝这类自然语言结论，要求执行恢复工具。

业务决定卡只允许以下情况：

- 多个字段都可能对应同一业务属性；
- 资料没有给出主键或业务口径；
- 用户目标存在互斥选择；
- 继续操作会扩大已批准范围。

工具未注册、服务版本不匹配等真实平台问题由 Core 生成确定性系统消息，不能由模型自由改写原因。

### 4.6 第六层：前端显示问题生命周期

后端为每个问题返回稳定 `issueId`，并发布以下生命周期事件：

```text
detected -> repairing -> resolved
detected -> blocked -> waiting_user
detected -> discarded
```

前端按 `issueId` 更新同一条状态，不再按每个 `toolCallId` 追加一张红色消息。

主对话只显示：

- 当前正在修正的问题；
- 当前需要用户处理的问题；
- 与本轮业务结果直接相关的最终摘要。

已经解决或被阻止的问题折叠到“活动记录”，并显示明确状态：

```text
已阻止，未写入
已自动修复
已由后续校验替代
仍需处理
```

“校验报告包含 N 项”是同一问题的摘要，不应再形成独立错误卡。恢复按钮使用结构化动作，
例如“使用对象绑定工具继续”“调整字段对应关系”“撤销本轮候选”，不再发送一条泛化的自然语言重试消息。

## 5. 重复与重叠工具整理

### 5.1 真正的旧版重复工具

| 旧工具 | 新工具 | 重叠程度 | 处理建议 |
| --- | --- | --- | --- |
| `upsert_object` | `sync_schema_objects`、`create_object`、`patch_object`、`patch_object_source_binding` | 高；旧工具还支持创建时直接绑定来源 | 新运行已隐藏；Core 按对象来源模式确定唯一入口，旧 handler 只供 checkpoint 兼容 |
| `upsert_rule` | `create_rule`、`patch_rule` | 高 | 新运行已隐藏，Core 保留 checkpoint 兼容 handler |
| `upsert_operation` | `create_operation`、`patch_operation` | 高 | 新运行已隐藏，Core 保留 checkpoint 兼容 handler |
| `bind_object_source` | `patch_object_source_binding` | 基本相同；新版增加计划、版本、Hash、幂等和回执 | 新运行只暴露新版，历史 checkpoint 暂时保留旧 handler |

当前没有旧版 `upsert_metric`，指标工具不需要做同类迁移。

旧工具不能只靠提示词标注“不要使用”。新运行已经从 OpenJiuwen ToolCard 中完全移除这些旧入口。
历史 checkpoint 为保持恢复时的工具 schema 一致，暂时使用 `legacy-v1` 并继续暴露历史工具；其调用仍经过新版 Core
安全门禁。新运行或重新开始的运行使用 `domain-v2`，不会继承该兼容面。

### 5.2 通用文件工具与领域工具的部分重叠

| 通用工具 | 重叠工具 | 是否删除 | 收敛方式 |
| --- | --- | --- | --- |
| `write_file` | `create_*`、`patch_*`、对象绑定、`stage_script_source` | 否 | 按路径和阶段限制为非受管文件 |
| `delete_file` | `preview_domain_delete`、`delete_domain_resource` | 否 | 领域路径必须转向带引用预览的结构化删除 |
| `list_dir`、`read_file`、`grep_package` | `list_domain_resources`、`get_domain_resource` | 否 | 普通文件保留；领域任务优先暴露结构化读取 |
| `who_references` | `preview_domain_delete` | 否 | 前者是只读一跳查询，后者生成与删除绑定的预览 token |

### 5.3 原子编排层与单项动作的重叠

`stage_change_set` 不是 `create_*`、`patch_*` 的重复实现，而是它们的原子批量编排层。
但模型同时看到两层工具，就可能绕过计划逐项写入。

新计划批准后：

- 模型只调用 `stage_change_set`；
- `create_*`、`patch_*` 作为 Change Set action 和 Core 内部 handler 保留；
- Schema 同步、对象重绑、删除和脚本作为带 `plan_task_id` 的受控例外工具暴露；
- 白名单补齐后，再隐藏这些单项例外入口。

### 5.4 名字相近但语义不同

| 工具组 | 结论 |
| --- | --- |
| `update_plan` 与 `create/get/revise_change_plan` | 前者已经改名为 `update_work_notes`；`update_plan` 只留给历史 checkpoint，后者仍是 Core 强制执行计划 |
| `define_script_contract`、`stage_script_source`、`run_script_test` | 分别定义契约、提交源码和执行测试，是必要的顺序阶段 |
| `get_validation_summary`、`get_validation_findings` | 分别读取稳定摘要和分页明细，不重复 |
| `analyze_package_merge`/`stage_package_merge` 与 package fusion 工具 | 前者处理单来源，后者处理多来源整体融合，应按来源数量确定性路由 |
| `preview_domain_delete` 与 `delete_domain_resource` | 预览引用影响和实际删除是两阶段安全协议 |

## 6. 目标工具可见性

| 场景 | 模型可见写工具 |
| --- | --- |
| 新建受控计划前 | `create_change_plan`、只读盘点工具 |
| 已批准的新计划 | `stage_change_set`，以及尚未纳入 Change Set 的受控例外工具 |
| 普通非受管文件 | 受路径限制的 `write_file`、`delete_file` |
| 历史 checkpoint 恢复 | `legacy-v1` 保持历史 schema，所有调用仍经过新版 Core 门禁和结构化错误转向 |
| Core 内部 | 新旧 handler 可暂时共存，但旧 handler 不注册为新 Agent ToolCard |

## 7. 迁移顺序

### 阶段一：补齐能力，不删除旧入口（已完成）

1. 使用 Schema 全量同步、虚拟对象 Change Set 和专家重绑三条互斥路径补齐对象来源能力；
2. 为受管工具拒绝增加 `requiredAction`、`requiredTool` 和 `issueId`；
3. 增加按 finding 身份持久化的修正状态；
4. 实现确定性恢复 Workflow。

### 阶段二：切换新运行工具面（已完成）

1. 新运行隐藏 `upsert_*` 和 `bind_object_source`；
2. 已批准计划隐藏单项 `create_*`、`patch_*` 调用入口；
3. `write_file`、`delete_file` 应用强路径白名单；
4. 能力声明 Rail 开始阻止错误的平台能力结论。

### 阶段三：迁移前端问题生命周期（已完成）

1. 后端开始发送稳定 `issueId` 和状态转换；
2. 前端按问题更新，不按工具调用追加；
3. 历史事件缺少 `issueId` 时只保留在活动记录，不把它重新渲染成当前阻塞；
4. 新恢复按钮改为结构化动作。

### 阶段四：逐步下线旧 handler（后续发布周期）

1. 统计历史 checkpoint 中旧工具名的剩余数量；
2. 历史 checkpoint 数量归零后关闭 `legacy-v1` 模型侧兼容 schema；
3. 经过一个发布周期后再评估删除 Core 旧 handler；
4. Core 路径门禁和派生文件一致性校验永久保留。

## 8. 改造前后变化

| 场景 | 改造前 | 改造后 |
| --- | --- | --- |
| 对象来源绑定 | 模型可能分别写对象和映射文件 | Core 按来源模式选择 Schema 全量同步、专家重绑或虚拟对象，并原子生成派生文件 |
| 模型选错工具 | 返回错误后由模型自由判断 | Rail 根据 `requiredTool` 确定性转向 |
| 旧 checkpoint | 新旧工具混杂且没有明确版本边界 | 使用 `legacy-v1` 保持 schema 兼容；新运行只使用 `domain-v2` |
| 多资源计划 | 模型可选择单项写或 Change Set | 已批准计划只允许 Change Set |
| 修正次数 | 不同错误共同消耗 3 次 | 按候选、校验器、错误类和指纹分别计数 |
| 技术失败 | 可能升级成用户业务决定 | 工具可用时禁止技术问题升级 |
| 前端错误 | 每次失败永久显示一张红卡 | 同一问题更新状态，历史结果折叠 |
| 恢复按钮 | 发送泛化“继续”文本 | 执行带问题身份和目标工具的结构化恢复 |

## 9. 验收用例

所有端到端用例继续遵守纯浏览器限制：测试执行阶段只能使用页面上的鼠标点击和文本输入，不能调用接口、
前后端脚本或数据库改变状态。故障场景由验收环境预置，测试项目和资料只使用纯合成数据。

| 用例 | 操作 | 通过条件 |
| --- | --- | --- |
| E2E-TOOL-01 | 正常生成包含 Schema 对象及数据表绑定的知识包 | Agent 使用 `sync_schema_objects`，不出现直接写映射文件 |
| E2E-TOOL-02 | 从旧 checkpoint 恢复一次 `bind_object_source` 调用 | `legacy-v1` 保持原 schema，旧 handler 受新版门禁约束，候选只生效一次 |
| E2E-TOOL-03 | Schema 同步首次返回受管文件结构化拒绝 | Agent 自动重试 `sync_schema_objects`，最终同步成功，不询问平台能力 |
| E2E-ATOMIC-01 | 同步 Schema 对象，故障注入在派生文件生成中途 | 整批回滚，不存在对象与映射不一致 |
| E2E-REPAIR-01 | 连续触发工具误用、阶段等待和格式 finding | 只有相同格式 finding 的重复失败消耗修正次数 |
| E2E-REPAIR-02 | 相同 finding 达到上限 | 只停止当前问题，其他候选不被冻结 |
| E2E-ASK-01 | 恢复工具已注册时模拟技术失败 | Agent 不得发送“平台无法处理”的业务决定卡 |
| E2E-ASK-02 | 两列都可能映射同一业务字段 | 显示真实业务歧义选项，用户选择后继续 |
| E2E-UI-01 | 一个文件先校验失败、随后修正成功 | 主对话只保留“已自动修复”，旧明细进入活动记录 |
| E2E-UI-02 | 一份报告包含三项 finding | 显示一个问题卡及三项明细，不出现重复摘要红卡 |
| E2E-UI-03 | 刷新并重新进入等待中的项目 | 当前阻塞和恢复动作保持一致，历史错误不会重新变成当前错误 |
| E2E-LEGACY-01 | 使用部署前保存的完整运行检查点恢复 | 不重复提交，不丢候选，不需要数据库人工修复 |

此外需要以下确定性测试：

- Core 单元测试：受管路径拒绝、绑定事务回滚、Hash 冲突、幂等回执、派生文件一致性；
- Agent 集成测试：旧错误自动转向、工具可用性门禁、按指纹计数、停止与恢复；
- 前端测试：问题合并、已解决状态、刷新回放、结构化恢复按钮；
- OpenJiuwen Workflow 测试：节点重放不重复写入，检查点恢复从未完成节点继续；
- 反过拟合测试：使用仓库外合成对象和表名验证工具路由不依赖固定关键词。

## 10. 当前卡住运行的恢复原则

当前卡住的运行不能通过直接编辑数据库或手工覆盖映射文件恢复。新版恢复路径是：

1. 从持久化 checkpoint 读取现有 Change Plan、候选、校验报告和技术恢复问题；
2. 当旧调用再次遇到受管文件拒绝时，按活动计划转换成 `schema_objects` 或 `object_binding` 恢复问题；
3. 保留已经生成且校验通过的候选资源；
4. Schema 生成执行全量同步，专家重绑只处理计划指定对象；
5. 完整重新校验候选；
6. 前端恢复按钮只提交 `issueId` 和 `actionId`，不伪造一条用户自然语言消息；
7. 仅在校验通过后展示统一确认卡；
8. 原问题状态标记为 `resolved`，历史失败保留在活动记录。

如果字段对应存在真实歧义，再让用户选择；不能要求用户等待一个实际上已经存在的工具能力。

## 11. 收益、代价与风险

### 收益

- 模型无法在重复工具之间选择低安全路径；
- 对象来源不会出现半套状态；
- 旧运行能够平滑恢复；
- 自动修正不会被无关错误提前耗尽；
- 用户可以明确知道哪些问题已经解决；
- 正常生成和人工增量修改复用同一套领域约束。

### 代价

- Schema 同步需要盘点全部 Sheet，专家重绑需要读取资源 Hash、表结构和字段；
- ToolCard 需要根据计划和阶段动态收敛；
- 旧 checkpoint 兼容适配至少保留一个发布周期；
- 前后端需要共同维护稳定问题身份和状态转换。

### 风险

- Schema 全量同步的影响范围较大，必须永久保留计划批准、候选确认和原子回滚；
- 自动字段匹配过度可能把业务歧义静默写错，因此歧义必须转人工决定；
- 错误地重置修正预算会恢复无限重试，因此开启新轮次必须绑定确定性进展；
- 历史事件没有稳定问题 ID，迁移期的前端聚合需要保守，不能把不同问题误合并。

整体效率预计提高。虽然正确路径多了少量读取和校验调用，但会消除长时间自由试错、重复校验和错误升级，
正常长流程的总调用数和人工介入次数都会下降。

## 12. 完成标准

只有同时满足以下条件，才能认为本方案完成：

1. 新运行的 Agent ToolCard 中不再出现旧 `upsert_*` 和 `bind_object_source`；
2. 已批准的 Change Plan 不能通过单项工具绕过 Change Set；
3. 受管对象来源只能通过 `sync_schema_objects` 或 `patch_object_source_binding` 修改，虚拟对象不得生成 mapping；
4. 对象与映射派生文件不存在可观察的中间不一致；
5. 工具误用不会消耗格式 finding 的修正预算；
6. 工具可用时，Agent 无法发送“平台无法处理”的技术结论；
7. 前端能够明确展示已阻止、修正中、已修复和仍需处理；
8. 新增浏览器用例全部通过，旧 61 条浏览器黑盒用例无回归，清单合计 62 条；
9. 当前类型的真实长流程能够从已有 checkpoint 恢复并进入候选确认；
10. 审计记录仍能还原所有工具拒绝和恢复步骤，但不会污染主对话。

## 13. 实现与验证记录

### 13.1 确定性测试

| 层级 | 验证内容 | 结果 |
| --- | --- | --- |
| Core | 回合后全量 TypeScript 测试，包含来源策略、受管路径转向、原子同步、计划依赖、代理权限和结构化恢复路由 | `1651/1651` 通过 |
| Agent | 回合后 Docker test target 全量 pytest，包含工具配置迁移、Workflow 转向、问题持久化、按指纹预算和恢复 | `153/153` 通过 |
| 前端 | Workbench smoke，包含问题按 `issueId` 原位更新、修正中状态和解决后收起 | 全部通过 |
| 类型 | Core TypeScript `typecheck` | 通过 |
| 验收计划 | 校验用例编号唯一、动作白名单以及禁止 API、页面脚本和存储旁路 | `27/27` 通过 |

### 13.2 纯浏览器黑盒测试

在目标分支的隔离 5101 环境执行完整清单，测试过程只通过页面完成注册、登录、场景选择、故障开关、项目创建、文本输入、
按钮点击、确认、停止和页面刷新。测试未直接调用业务 API、前后端脚本或数据库，使用的项目和资料均为合成数据。

- 用例清单：旧 61 条加 `E2E-TOOL-03`，合计 62 条；
- 严格重复：模型超时、子任务超时、提交故障、回执丢失、重复 finding、正常生成和停止评测按规则重复；
- 实际执行：86 次；
- 结果：62 条通过、0 条失败、0 次失败尝试；
- 新场景结果：第一次 Schema 对象同步被结构化拒绝后，Agent 自动重试 `sync_schema_objects`，最终候选确认成功；
- 页面结果：没有出现“当前环境无法”或受管映射错误残留，桌面、1024px 窄屏和 390px 手机页面无横向溢出或控件遮挡。

### 13.3 部署边界

原独立实现曾在隔离的 5102 环境完成验证；本次回合后的最终验证在目标分支专用 5101 环境完成，结果以本节
最新记录为准。cs 机器现有 5100 服务及其数据均未修改，本次回合不替换生产入口。
