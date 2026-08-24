# 后端设计 · 纯 agent 建模运行时

> 状态：设计草案 · 2026-08-22
> 配套：[前端草图](01-frontend-mockup.html) · [功能地图](02-feature-map.md) · [用户旅程](03-user-journeys.md) · [技术选型与实施](04-tech-stack-and-implementation.md)

本文是后端的权威设计。功能地图回答「有什么」，技术选型回答「用什么建」，本文回答**「跑起来是什么样」**。

---

## 1. 理念与两条判据

**尽量基于 openJiuwen agent，去掉硬码。**

落地时靠两条判据，方向相反、互相约束：

| 判据 | 用途 |
| --- | --- |
| 凡是「agent 怎么跑」的问题，交给 openJiuwen | 防止自建框架 |
| 涉及安全边界、不可逆操作、对外承诺、或「模型会漏且后果静默」的，必须硬码 | 防止去硬码把不变量一起去掉 |

第二条的完整清单见 [功能地图 §9](02-feature-map.md)。没有它，「去硬码」会一路滑到没有系统。

---

## 2. 状态住在哪

四处，各有明确归属，不重叠：

| 位置 | 内容 | 谁写 | 谁读 |
| --- | --- | --- | --- |
| **git 工件仓库** | 知识包全部内容 + `.modeling/`（goal / plan / analysis / decisions / provenance） | agent 经 `write_file` | agent、前端、审计 |
| **openJiuwen execution session** | 单次 run 内的对话、工具边界和 checkpoint；每个新 run 使用独立 session，避免原文历史无限增长 | 框架 | 框架 |
| **Agent SQLite** | `project_agent_state` 唯一当前状态，以及 run、交互、事件和状态转换事实 | Agent 运行时 | Agent 运行时、前端 |
| **core SQLite** | 候选工作区、确认记录、资料索引和评测记录，不保存另一份 Agent 当前状态 | core | core、前端 |

**不自建第二份模型会话序列化。** openJiuwen checkpointer 保存单次 execution session；我们只在 `project_agent_state.checkpoint_id` 记录项目当前恢复点。现有 `authoring_runs` 存 messages 数组的做法不进入新运行时。

**当前状态只存一处。** Agent SQLite 的 `project_agent_state` 每项目一行，`state` 统一编码业务阶段与执行状态，例如 `goal:waiting`。同一行还保存 CAS `revision`、当前 `run_id`、`interaction_id`、`checkpoint_id`、精简摘要、开放问题和错误。`phase`、`execution` 只是 API 返回时从 `state` 派生，不单独落库。

`project_agent_state_transitions` 只追加审计；`agent_runs`、`agent_interactions`、`agent_events` 和 openJiuwen checkpoint 是事实或恢复材料，不参与判断项目当前状态。core 不建立对应的 workflow 状态表，也不存在 `.modeling/state.md`。新 run 读取唯一状态和知识包事实；原文默认按需分页读取，显式全文精读的材料按覆盖账本并发读取全部可解析单元，不复用上一轮的完整长对话历史。

---

## 3. 一次 run 的生命周期

```
用户点「开始建模」
 ├─ core 取单写者租约（失败 → 页面进只读模式，显示「XX 正在建模」）
 ├─ 有未答交互？
 │   ├─ 是 → 按绑定表找到 checkpoint，重建 session
 │   └─ 否 → 新 run、新 execution session，注入项目业务状态并按需读 plan / 证据
 └─ Leader 循环（openJiuwen ReActAgent 或 Swarm Leader）
     每轮：思考 → 调工具 → 观察
      ├─ Pre-hook 拦截不安全写入（路径穿越、key 重复）→ 拒绝并回可执行错误
      ├─ write_file 落候选修订工作区，**不 commit**
      └─ 遇到 ask / request_confirmation
          ├─ core 落库 interaction
          ├─ 框架 checkpoint
          ├─ 写绑定表
          └─ run 挂起（租约不释放，带超时）

用户应答
 ├─ core 写 answer
 ├─ agent 服务从 checkpoint 恢复
 └─ 答案作为那次工具调用的返回值注入，循环继续

request_confirmation 被批准
 ├─ core 把候选工作区 commit 成一个修订
 ├─ 写 confirmation 记录（修订 + 用户 + 角色 + 时间 + 变更组）
 └─ 门禁重算
```

### 3.1 即时响应与流式输出

用户动作和 Agent 输出是两条不同的时延路径，必须分别保证：

| 路径 | 契约 |
| --- | --- |
| 发送消息、选择问题选项、提交确认 | 点击处理器在等待 HTTP 前先把用户消息或已答卡片写入同一消息流；请求失败再移除乐观消息并恢复原卡片 |
| Agent 自然语言回答 | 使用 openJiuwen `Runner.run_agent_streaming`，每个 `llm_output` chunk 立即持久化并发布 `message_delta`，结束时发布一次 `message_done` |
| 服务转发 | Agent 和 core 代理都返回 `text/event-stream`、`Cache-Control: no-transform`、`X-Accel-Buffering: no`；core 逐段 pipe，不聚合完整回答 |
| 断线恢复 | `agent_events` 是耐久事件日志；浏览器用最后一个 `seq` 重连，增量按 `messageId` 合并，重复事件丢弃 |

自然语言只走流式 Runner，不再通过一次性 `reply` 工具旁路。进度、问题、diff、确认和增量回答仍然只有一条事件流；`message_delta` 只改变流中同一条消息的文本，不建立第二个“流式输出窗口”。输入框不依赖 run 状态禁用，Agent 输出时仍可插话。

验收同时看协议和页面：Runner 测试必须观察到多个 delta 且无重复完整消息；core 代理测试必须在上游结束前收到第一段；真实浏览器从原生 click 到本地消息或卡片状态变化必须小于 250ms，并观察到同一条 Agent 消息至少两次长度增长。

### 三个不显然的决定

**写入不 commit，确认才 commit。** agent 一轮可能改十次文件，若每次都 commit，修订历史会变成噪音、回退失去意义。候选工作区是廉价的，`request_confirmation` 通过时才落成一个语义完整的修订。

**挂起不释放租约。** 用户走开半小时回来，项目仍归他写。租约带超时（默认 30 分钟），超时后其他人可接管，接管时明确提示有未答问题。

**恢复不重放。** 从 checkpoint 续跑，此前的工具调用不再执行。这是 `CheckpointerFactory` 的语义，不要在外面再造一层重放逻辑。

---

## 4. 流程控制：三层承载

现有 `orchestrator.ts` 的根本问题是把三种不同的东西混成一种、全用代码实现，于是方法论的每次微调都要改代码。拆开：

| 类型 | 内容 | 承载 | 可否被覆盖 |
| --- | --- | --- | --- |
| **不变量** | 门禁顺序、写入必走候选、路径安全、确认必须人工 | **工具的前置条件检查** | 不可 |
| **方法论** | 先目标后流程、抽取按流程分批、冲突要裁决、怎么问人 | **Swarm Skill** | 可 |
| **机械管道** | 打包→导入→配数据源→逐用例→judge | 普通后端代码 | 不涉及 |

### 4.1 不变量：前置条件下沉到工具契约

这是替代编排器的核心机制。

传统编排器是上帝视角：它知道现在该做什么，不让你做别的。要枚举全部合法路径，复杂度 O(路径数)。

新方式是每个工具知道自己的前置条件，调错了返回一个**可执行的错误**：

```json
{
  "error":  "GATE_UNMET",
  "gate":   "process_confirmed",
  "reason": "该 operation 引用的 diagrams/采购申请.mmd 尚未确认",
  "next":   ["request_confirmation(change_groups=['采购申请流程'])",
             "或写入 .modeling/analysis/ 作为草稿"]
}
```

agent 不需要编排器告诉它顺序，它撞到墙就知道墙在哪，而且墙会告诉它门在哪边。这比编排器更鲁棒：编排器必须预先枚举所有合法路径，前置条件只需要定义局部约束，复杂度 O(操作数)。**这是 65k 行能塌到 5k 行的数学原因。**

### 4.2 全部前置条件

这张表就是全部流程控制。约 20 条规则，取代 3137 行编排器，且每条是一个独立单元测试。

| 工具 | 前置条件 | 违反时返回 |
| --- | --- | --- |
| `write_file` `delete_file` | 持有单写者租约 | `LEASE_LOST` |
| `write_file` `delete_file` `import_zip` | 路径与 key 合法（5 条写入安全，§5） | `WRITE_UNSAFE` |
| `write_file(operations/*)` | 引用的流程已确认 | `GATE_UNMET: process_confirmed` |
| `write_file(scripts/*)` | 契约已确认 且 沙箱测试通过 | `CONTRACT_REQUIRED` |
| `request_confirmation` | 存在候选修订 | `NO_CANDIDATE` |
| `request_confirmation` | 变更组依赖闭合（不产生引用断裂） | `BROKEN_GROUP_SELECTION` + 需补入的组 |
| `request_confirmation` | 每个变更组标了 `tier`，且与实际改动的目录相符 | `TIER_MISMATCH`（改了 `rules/` 却标 `index`） |
| `request_confirmation` | 变更组数 ≤ 5 | `TOO_MANY_GROUPS` + 建议拆分方式 |
| `run_script_test` | 脚本有 entrypoint 与输入输出契约 | `CONTRACT_REQUIRED` |
| `run_evaluation` | `gate_status('ready_for_eval').pass` | `GATE_UNMET` + blockers |
| `merge_apply` | 全部冲突已裁决 | `UNRESOLVED_CONFLICTS` + 未裁决清单 |
| `publish` | `gate_status('releasable').pass` | `GATE_UNMET` + blockers |
| 全部工具 | 项目未被删除、用户有对应角色 | `FORBIDDEN` |

**可执行错误的格式约定**：`error` 是稳定错误码，`reason` 是给人和模型读的一句话，`next` 是**具体可调用的动作**而不是建议性描述。写不出 `next` 的检查，说明它不该阻断。

### 4.3 方法论：Swarm Skill，按需加载

Swarm Skill 定义一支团队——有哪些角色、各自负责什么、如何协作、遇到问题怎么处理。建模方法论住在这里，不住在代码里。

| Skill | 加载时机 |
| --- | --- |
| `modeling-leader` | 常驻主循环 |
| `material-analysis` / `knowledge-extraction` | 派发对应 Teammate 时 |
| `package-merge` | 首次调 `detect_conflicts`，由工具返回值里的 `see_skill` 触发 |
| `incremental-intake` | 首次调 `who_references` 触发 |
| `failure-repair` | 评测失败后触发 |

**让工具返回值触发 Skill 加载**，而不是开局把全部方法论塞进系统提示。一开局就塞满会挤掉真正的工作上下文。

#### 写进 `modeling-leader` Skill 的硬约定

这些是方法论而非不变量，但影响体验，必须写死在 Skill 里：

- **一次 `request_confirmation` 不超过 5 个变更组。** 超过就拆多轮。前端的分诊是兜底，不是常态——17 项的确认请求本身就是设计失败。
- **每个变更组必须标 `tier`**（`caliber` 业务口径 / `structural` 结构 / `index` 索引同步）。标错会被 core 拒绝。
- **口径类变更单独成组**，不能和结构变更混在一个组里——混了用户就只能整组接受或整组退回。
- **发现资料缺口时主动 `ask(kind='material_request')`**，并且必须给出「我口述」这个出口，不能只要文档。

工具返回值里带一个可选字段：

```json
{ "conflicts": [...], "see_skill": "package-merge" }
```

### 4.4 机械管道：不用 SwarmFlow 编排建模

判据：**当一个阶段序列里没有任何一步需要根据上一步的内容决定下一步做什么，才用 SwarmFlow。**

- 评测执行管道：符合，但它已经是普通后端代码，改成 SwarmFlow 没有收益
- 资料分析扇出：符合，但 Swarm 的 Teammate 派发已经覆盖
- 融合：**不符合**——冲突类型决定要不要问人，用户回答可能改变导入范围
- 建模主干：完全不符合

**结论：v1 不用 SwarmFlow 编排建模主干。** 真正的风险是组织性的：一旦手上有了「可以写确定性阶段脚本」的能力，团队会忍不住把建模流程再写进去一遍，半年后得到一个 Python 版的 `orchestrator.ts`。这条连同引入条件一起写进决策记录，不靠自觉。

---

## 5. 写入安全（Pre-hook）

v1 删除全部校验器，但以下 5 条跟着写入路径走。它们不是校验，是写入函数自己的参数合法性检查，共同点是**失败静默且不可逆**：

| 检查 | 现有位置 | 后果 |
| --- | --- | --- |
| key 含 `..` | `keys.ts:32` | 路径穿越 |
| key 含 `/ \ # ? & \|` 或控制字符 | `keys.ts:33-34` | 路径穿越、文件名破坏 |
| 同类资源 key 重复 | `keys.ts:41` | **详情文件被静默覆盖**（静默丢数据） |
| zip 条目路径含绝对路径 / 反斜杠 / `..` / 空段 | `keys.ts:90-96` | 解压穿越 |
| script entrypoint 非 `.py` 或含 `..` | `spec-validation.ts` | 路径穿越 |

以 **PreToolUse hook** 实现（openJiuwen `AgentCallbackEvent`，`single_agent.rail.base`），失败直接拒绝、不产生脏数据。用 Pre 而不是 Post：Post 发现问题时已经落盘。

### 5.1 校验的三个位置

即使后续按判据加回检查，也要按位置分工，不能全塞进 hook：

| 位置 | 跑什么 | 失败行为 | 理由 |
| --- | --- | --- | --- |
| **Pre-hook** | 单次写入的路径 / key 合法性 | 拒绝写入 | 无跨文件依赖、无中间态，失败即真失败 |
| **工具 `validate`** | 引用完整性等一致性检查 | 返回结果 | 天然跨文件、天然有中间态，时机与 scope 必须由 agent 定 |
| **门禁** | 状态跃迁的前置条件 | 阻断跃迁 | agent 可以一直不调 validate，但想让用户确认时必须过 |

**「agent 会不会忘记校验」的答案是门禁，不是 hook。** 全放 PostToolUse hook 有两个后果：agent 先写 operation 再写它引用的 rule 是合理路径，中间必然出现悬空引用，hook 会在每一步尖叫；一次建模几百次写入，每次注入校验结果会把上下文刷爆。

---

## 6. 与 openJiuwen 的职责边界

| 能力 | 归属 | 具体 |
| --- | --- | --- |
| 模型客户端、provider 适配、重试 | openJiuwen | `ModelClientConfig` / `ModelRequestConfig` |
| ReAct 循环、轮次上限 | openJiuwen | `ReActAgent` / `max_iterations`（**200 量级，不是 12**） |
| 上下文窗口 | openJiuwen | `ContextEngine` / `ContextEngineConfig` |
| 会话与检查点 | openJiuwen | `create_agent_session` / `CheckpointerFactory` |
| 团队分解、Teammate 派发、汇总 | JiuwenSwarm | Leader / Teammates（Cluster 模式） |
| 工具审批策略 | JiuwenSwarm | tool permission，我们只给一张配置表 |
| 脚本沙箱 | JiuwenBox | 与 consumer 同一套安全策略 |
| 事件流 | openJiuwen | `AgentCallbackEvent` + `write_invoke_result_to_stream`，我们只做转换与脱敏 |
| — | — | — |
| 知识包是什么（结构、修订、候选、确认） | **我们** | `core/` |
| 什么绝对不允许（写入安全、门禁） | **我们** | Pre-hook + `gates.ts` |
| 建模方法论 | **Skill 文本** | 不是代码 |

### 6.1 工具审批分级

JiuwenSwarm 默认每个工具执行前都要人审批。一次建模几百次工具调用，这个默认值会毁掉体验，必须按级配置：

| 级别 | 工具 | 审批 |
| --- | --- | --- |
| 只读 | 资料检索、`gate_status`、`who_references`、`diff` | 免 |
| 写候选 | `write_file`、`delete_file`、`update_plan` | 免 |
| 项目状态 | `update_project_context`、`transition_project_state` | 免工具审批；Agent 运行时强制 CAS 与合法边，转换前调用 core 确定性门禁 |
| 花钱 / 外部 | `run_evaluation`、`run_script_test`、`query_eval_db` | 免，计入预算闸门 |
| 改变已确认状态 | `request_confirmation`、`publish` | **必须人**——这就是 HITL |

`write_file` 之所以能免审批，是因为它写的是候选修订。**审批点从「每次写」收敛到「确认候选」，这是让审批可承受的关键设计。**

---

## 7. 前端支撑契约

前端的几处交互设计对后端有硬要求，列在这里避免实现时漏掉。

### 7.1 工具返回值的 `display` 字段

对话流底部的活动行要说人话（「正在读《安全库存管理办法 v3》· 第 3/6 份」），**不能让前端解析工具名拼文案**。每个工具返回值带：

```json
{
  "display": { "text": "正在读《安全库存管理办法 v3》", "progress": [3, 6] },
  "...": "工具本身的返回"
}
```

`progress` 可选。并行的 Teammate 由 bridge 层合并成一条（「3 个分析并行中，2 完成」），不逐个显示。

### 7.2 变更组的 `tier` 与批量确认

```json
{
  "change_groups": [
    { "id": "g1", "tier": "caliber",    "title": "新增规则「安全库存判断」",
      "impact": ["operations/生成采购建议.md", "diagrams/采购申请.mmd"],
      "warning": "这会改变已有查询结果" },
    { "id": "g2", "tier": "structural", "title": "备件对象新增 3 个字段" },
    { "id": "g3", "tier": "index",      "title": "objects/index.md 同步" }
  ]
}
```

`tier` 由 agent 标注，core 校验其与实际改动目录相符（见 §4.2）。前端据此分诊：`caliber` 默认展开、`structural` 折叠成一行、`index` 隐藏进计数。

`warning` 是可选的一句话，用于「这会改变已有查询结果」这类提示——它比 diff 本身更能让专家停下来看。

### 7.3 复核方式必须留痕

确认记录新增两个字段，审计要能区分「逐条看过」和「抽检通过」：

```
confirmations(
  ...,
  review_mode,      -- 'full' | 'sequential' | 'sampled'
  sampled_groups    -- review_mode='sampled' 时实际展示给用户的组
)
```

**抽检是有意提供的**：用户不可能真看 17 条，与其假装他看了，不如随机抽检并诚实记录。发布摘要里会体现哪些确认是抽检通过的。

### 7.4 确认策略的归属（修正）

之前把确认策略整体归给 JiuwenSwarm tool permission，**这一处过头了**。两者管的不是一个粒度：

| 粒度 | 归属 | 例子 |
| --- | --- | --- |
| 工具级：这个工具要不要审批 | JiuwenSwarm tool permission | `run_evaluation` 免审批，`publish` 必须人 |
| **变更组级：这个工具的这部分参数要不要审批** | **core 的 `request_confirmation` 实现** | 「索引同步不用问我」 |

Swarm 不知道「变更组」是什么，也不该知道。core 收到 `request_confirmation` 时按项目策略自动批准某些 `tier`，只把需要人看的返回给前端。约 30 行，不构成"自建策略引擎"。

策略表：

```
confirm_policy(project_id, tier, mode)   -- mode: 'ask' | 'auto'
```

`caliber` 和 `script` 两档的 `mode` 硬编码为 `ask`，不接受写入——那是核心里的硬约束，不是策略。策略变更**不回溯**已生效的确认记录。

### 7.5 资料的三个时间点

| 时间点 | 入口 | 后端 |
| --- | --- | --- |
| 项目创建时 | 全屏上传页 | 现有上传接口 |
| **建模过程中** | 对话流里的资料请求卡 | `ask(kind='material_request')` 挂起；用户上传或口述后恢复 |
| 建模完成后 | 增量会话 | 同上，走增量路径 |

`material_request` 是 `ask` 的一个 kind，**不新增挂起原语**——保持只有 `ask` 和 `request_confirmation` 两个的纪律。

用户选「我口述」时，回答写入 `.modeling/decisions/<n>.md`，evidence 前缀 `user-decision:`，与文档证据同等对待。**这条出口不能省**：很多领域知识只在专家脑子里，堵死它用户只能放弃。

### 7.6 双向反查端点

资料界面最有价值的功能，两个只读 GET（core，不是 agent 工具）：

| 端点 | 用途 |
| --- | --- |
| `GET /api/projects/:id/evidence/locate?ref=material:X%23§3.2` | evidence 引用 → 资料文件、偏移、原文片段 |
| `GET /api/projects/:id/materials/:mid/referrers?anchor=§3.2` | 资料段落 → 引用它的知识条目清单 |

第二个复用 `who_references` 的实现，只是入口从知识条目换成资料锚点。

删除资料前必须调用 referrers 并把数量返给前端——「删除会让 14 条知识失去证据」这句提示是删除操作的前置条件。

---

## 8. 失败与恢复

| 情况 | 行为 |
| --- | --- |
| 用户关掉浏览器 | run 在服务端继续，遇 `ask` 挂起 |
| agent 服务重启 | 从最近 checkpoint 续跑，不重放此前工具调用 |
| core 服务不可用 | 工具调用返回 `CORE_UNAVAILABLE`，agent 挂起并告知用户，不空转重试 |
| 上下文压缩触发 | 新 execution session 注入项目唯一业务状态；agent 按需重读 plan 与证据 |
| agent 绕圈 | 无进展保护：连续 N 轮无写入且无新证据 → 中止并报告已尝试的动作 |
| 超出 token 预算 | 挂起并告知用户，由用户决定继续还是收敛范围 |
| 租约超时 | 其他人可接管，接管时提示存在未答问题 |
| 用户在 agent 跑的时候插话 | 消息在下一个工具边界注入；未答问题保持挂起，答完插话后重新推到流底 |

**没有自动重试循环。** 现有「自动补交 2 次」那类机制随校验器一起删除——agent 看到工具返回值自己决定要不要重试，比外部计数器合适。

---

## 9. 门禁（v1）

门禁是纯谓词，不推进任何东西，只回答能不能过。v1 只依赖三类真实事实：**确认记录、候选状态、真实执行结果**。

| 门禁 | 谓词 |
| --- | --- |
| 资料就绪 | 至少 1 份资料解析成功；解析失败立即告警但不阻塞其他可读资料建模 |
| 建模目标确认 | `.modeling/goal.md` 存在且有绑定修订的确认记录 |
| 业务流程确认 | 每条 `diagrams/*.mmd` 有确认记录 |
| 知识结构确认 | 知识结构有确认记录 |
| 确定性能力确认 | 每个 script 有契约、**沙箱测试通过**、绑定 operation |
| 建模完成确认 | 无待确认候选 且 无未答 `ask` |
| 进入验证 | 建模完成确认 |
| 正式发布 | 全部用例执行完成且 pass ∧ 无待确认候选 ∧ 版本号已确认 |

沙箱测试和全量用例通过是 v1 仅有的两道真实质量闸门。它们不是校验器——是真的把代码跑起来、把问题问一遍。

门禁**顺序是硬的**，通往门禁的路径是软的。这是唯一不让 agent 碰的流程结构。

---

## 10. 开放问题

| # | 问题 | 影响 | 何时定 |
| --- | --- | --- | --- |
| 1 | JiuwenSwarm 的 HITL（`human` / `human_session`）能否承载 `ask` / `request_confirmation` 的挂起语义 | 决定用 Swarm 还是退化到 `ReActAgent` + 薄 `spawn` | P1 spike |
| 2 | Leader/Teammate 的工具面能否按角色差异化注入 | 同上 | P1 spike |
| 3 | Cluster 模式下 checkpoint 的粒度 | 影响恢复语义与绑定表设计 | P1 spike |
| 4 | agent 写出的 `.mmd` 语法错误如何暴露 | v1 无校验器，可能前端渲染白屏 | P3，倾向于「前端渲染失败即显示错误，交给用户反馈」 |
| 5 | 单写者租约的超时值与接管交互 | 多人协作体验 | P3 |
