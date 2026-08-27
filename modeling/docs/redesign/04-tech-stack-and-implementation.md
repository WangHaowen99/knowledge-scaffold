# 技术选型与实施 · 纯 agent 建模工作台

> 状态：设计草案 · 2026-08-22
> 配套：[前端草图](01-frontend-mockup.html) · [功能地图](02-feature-map.md) · [用户旅程](03-user-journeys.md) · [后端设计](05-backend-design.md)
>
> 本文回答「用什么建、怎么分期」。运行时设计见[后端设计](05-backend-design.md)。

---

## 1. 选型结论

| 层 | 选择 | 理由 |
| --- | --- | --- |
| Agent 框架 | **openJiuwen `agent-core`（Python）** | 已定；`consumer/backend` 已在生产使用 `openjiuwen==0.1.14`，集成经验和踩坑记录可直接复用 |
| 多 agent 编排 | **JiuwenSwarm**（Leader + Teammates，Cluster 模式） | 建模天然是 leader 分解 + 专业角色并行；SwarmFlow 的 HITL 和工具审批与本设计的 `ask` / `request_confirmation` 同构 |
| 脚本沙箱 | **JiuwenBox** | consumer 已用它跑 ontology 脚本，建模侧的受控脚本测试直接复用同一套安全策略 |
| Agent 运行时语言 | Python 3.11（`>=3.11,<3.14`） | openJiuwen 硬约束 |
| 确定性核心 | **保留现有 Node/Fastify** | 8–10k 行有测试的资产，重写风险远大于收益 |
| 前端 | React 19（沿用），重写工作台外壳 | 组件层大改，构建链不动 |
| 存储 | SQLite + isomorphic-git 工件仓库（沿用） | 不变 |
| **不自建的** | 模型客户端、agent 循环、上下文裁剪、检查点、子 agent 编排、工具审批、事件流、脚本沙箱 | 全部由 openJiuwen / JiuwenSwarm / JiuwenBox 提供，见 [功能地图 §2.4](02-feature-map.md) |
| 进程间协议 | localhost HTTP + SSE | 见 §3 |

### 被否决的方案

| 方案 | 否决理由 |
| --- | --- |
| 一步重写为 Python 单体 | 与架构改造同时进行会让两处风险叠加：改架构期间失去稳定的产物比对基线。见 §2.4 的两步路径 |
| 在 Node 侧自建 agent 循环，不用 openJiuwen | 与已定的框架选型冲突，且放弃 checkpoint、ContextEngine、Swarm 编排等现成能力 |
| 用 openJiuwen 的 `WorkflowAgent` 编排建模 | 那是预定义多步流程，等于把刚删掉的编排器换个地方重写一遍 |

---

## 2. 系统拓扑

```
                    ┌──────────────────────────┐
                    │  前端 React 工作台         │
                    └───┬──────────────────┬───┘
             SSE 事件流 │                  │ 只读 GET（树/文件/diff）
                        │                  │
        ┌───────────────▼──────┐    ┌──────▼────────────────────┐
        │ modeling-agent (Py)   │    │ modeling-core (Node)      │
        │ :5110                 │───▶│ :5100                     │
        │                       │HTTP│                           │
        │ · Leader ReActAgent   │工具│ · 工件仓库 (git)           │
        │ · 子 agent (Swarm)    │调用│ · 写入安全（路径/key）       │
        │ · session/checkpoint  │    │ · 门禁谓词                 │
        │ · ContextEngine       │    │ · who_references（一跳）    │
        │ · 工具桥 + 审批        │    │ · 冲突检测（融合）          │
        │ · 事件流发布           │    │ · 评测管道 / consumer 客户端│
        └───────┬───────────────┘    │ · 文档解析 / 数据字典       │
                │                    │ · 认证 / 单写者租约         │
        ┌───────▼───────┐            └───────────────────────────┘
        │ JiuwenBox 沙箱 │
        └───────────────┘
```

**边界的价值不只是复用代码。** agent 进程**没有知识包目录的文件系统写权限**，只能通过 core 的 API 写入。校验、证据强制、修订不可变这些不变量由进程隔离物理保证，不靠 prompt 自觉。这是把工具边界做成网络边界的主要收益。

localhost HTTP 每次工具调用约 1–3 ms，相对一次 LLM 调用可以忽略。

### 2.4 双语言是过渡态，终态是全 Python 双进程

Node 后端最终应当完全去掉。理由：openJiuwen 是 Python 且不可协商；consumer 已是 Python，共享 observability、JiuwenBox、部署与认证栈价值可观；实测下来**没有 JS 生态锁定**——

- `flowchart/mermaid-import.ts`（673 行）不依赖 mermaid 包，是自写解析器，直译即可；它只用于服务端提取节点/边 ID，浏览器展示统一使用官方 mermaid.js
- `@lezer/python` 只用于 `script-safety.ts`，而 v1 该模块直接删除；即便保留，Python 内置 `ast` 更好
- `isomorphic-git`→`dulwich`、`xlsx`→`openpyxl`（consumer 已装）、`jose`→`PyJWT`、`neo4j-driver`→官方 Python driver、`fastify`→FastAPI，全部有成熟对应物

**但不要和架构改造同时做。** 分两步：

**第一步（P0–P2）** Node 保留，只暴露工具 API。删掉编排代码后它已从 49k 缩到约 17k。用它作为稳定基线验证新架构。验证点是 P2 验收。

**第二步（P4 之后，约 10–12 周，1 人，可与 P4/P5 重叠）** 按风险倒序搬：

| 批次 | 模块 | 行数 | 周 |
| --- | --- | --- | --- |
| 易 | `documents` / `settings` / `auth` / `db` / consumer 客户端 / `projects` / `nl2sql` | ~3,300 | 2 |
| 中 | `artifacts`（含 `package-store` 2283）/ `graph-import` / `evaluation` 保留部分 | ~7,100 | 4 |
| 难 | `package-render` / `package-draft-parser` / `mermaid-import` / `flowchart/document` | ~2,000 | 3 |
| — | 双跑验证 + Node 下线 | — | 2 |

原本"难"的那一档里最risky的 `spec-validation` + `package-check`（约 1,000 行）**因为 v1 全删校验器而不再需要迁移**，这一步的风险显著低于最初估计。

**语言统一，进程不统一。** 即使全 Python，也保持 `modeling-agent` 与 `modeling-core` 两个进程：agent 进程没有知识包目录写权限这条不变量，靠进程隔离物理保证；变成同进程 import 后就退化成模块约定，一次误用 `open(path, "w")` 就绕过去了。多一个部署单元换一条物理保证，划算。

---

## 3. openJiuwen 落地

以下 API 均为 `consumer/backend/agent/core/jiuwen_engine.py` 中已验证可用的形态。

### 3.1 Leader agent 构造

```python
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.context_engine import ContextEngineConfig

model_client_config = ModelClientConfig(
    client_provider="OpenAI",          # OpenAI-compatible 端点
    api_key=cfg.api_key,
    api_base=cfg.base_url,
    timeout=cfg.timeout,
    max_retries=3,
)
model_request_config = ModelRequestConfig(
    model=cfg.model, temperature=0.2, max_tokens=cfg.max_tokens,
)
context_engine_config = ContextEngineConfig(
    max_context_message_num=None,
    default_window_round_num=cfg.context_rounds,
    enable_reload=False,
    enable_kv_cache_release=False,
)
react_config = ReActAgentConfig(
    model_client_config=model_client_config,
    model_config_obj=model_request_config,
    model_name=cfg.model,
    model_provider="OpenAI",
    max_iterations=200,                # 关键：建模是长循环，不是 12 轮填表
    prompt_template=[{"role": "system", "content": MODELING_LEADER_PROMPT}],
    context_engine_config=context_engine_config,
)
agent = ReActAgent(card=AgentCard(
    id="modeling_leader", name="modeling_leader",
    description="知识包建模 Leader",
)).configure(react_config)
```

**`max_iterations` 是这次改造在代码上最小、语义上最大的一处改动。** 现状是 `MODELING_AGENT_MAX_TURNS=12`（`modeling/src/workflow/agent.ts:67`），因为模型只需要填完一张表。新设计里它要跑几十到几百轮工具调用。配一个足够大的上限 + 无进展保护（连续 N 轮没有产生任何写入或新证据则中止并报告），而不是靠小上限兜底。

### 3.2 会话与检查点

```python
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory

session = create_agent_session(session_id=f"project:{project_id}:run:{run_id}", card=agent.card)
await session.pre_run(inputs=inputs)
await agent.context_engine.create_context(session=session, history_messages=history)
```

映射关系：

| 本设计概念 | openJiuwen 对应 | 补充 |
| --- | --- | --- |
| 一次建模 run | 一个全新 agent session（`session_id = project:<id>:run:<run-id>`） | 项目同一时刻只允许一个活跃 run；新 run 不继承上一轮完整长上下文 |
| 检查点 | session checkpoint | **额外绑定知识包修订号**，见 §5 |
| 挂起等人 | 工具 `ask` / `request_confirmation` 返回前不 resolve | run 停在工具边界，checkpoint 落盘 |
| 恢复 | 从 checkpoint 重建同一 run 的 session + 读取 `project_agent_state`/`plan.md` | 不重放此前工具调用 |
| 上下文压缩 | ContextEngine window | 压缩后 agent 重读唯一项目状态和必要证据 |

### 3.3 工具桥

openJiuwen 工具是一个带 `async invoke` 的对象：

```python
class CoreTool:
    """把 modeling-core 的一个 HTTP 端点暴露成 openJiuwen 工具。"""
    def __init__(self, spec: ToolSpec, client: CoreClient, ctx: RunContext):
        self.name, self.description, self.parameters = spec.name, spec.desc, spec.schema
        self._client, self._ctx = client, ctx

    async def invoke(self, tool_args, session=None) -> str:
        if self._ctx.policy.requires_approval(self.name, tool_args):
            await self._ctx.request_approval(self.name, tool_args)   # 挂起
        result = await self._client.call(self.name, tool_args, run=self._ctx.run_id)
        await self._ctx.publish(ToolCallEvent(self.name, tool_args, result.summary))
        return result.json()
```

三件事都在这一层完成：**工具审批**（JiuwenSwarm 的 tool permission 语义）、**事件发布**（前端过程抽屉的数据来源）、**脱敏**（返回值里不带完整文档正文和密钥）。

**工具审批的默认值必须改。** JiuwenSwarm 默认每个工具执行前都要人审批；一次建模几百次工具调用，这个默认值会毁掉体验。按工具分级：

| 级别 | 工具 | 审批 |
| --- | --- | --- |
| 只读 | 资料检索、`gate_status`、`who_references`、`diff` | 免 |
| 写候选 | `write_file`、`delete_file`、`update_plan` | 免（落候选修订，可整体退回） |
| 项目状态 | `update_project_context`、`transition_project_state` | 免工具审批，但由 CAS、合法边和确定性门禁约束 |
| 花钱 / 外部 | `run_evaluation`、`run_script_test`、`query_eval_db` | 免，但计入 token / 次数预算闸门 |
| 改变已确认状态 | `request_confirmation`、发布 | 必须人——这就是 HITL 本身 |

`write_file` 之所以能免审批，是因为它写的是候选修订。审批点从"每次写"收敛到"确认候选"，这是让审批可承受的关键。

### 3.3.1 写入安全 Pre-hook

5 条写入安全检查（见 [功能地图 §3.7](02-feature-map.md)）以 **PreToolUse hook** 实现，落点是 openJiuwen 的 `AgentCallbackEvent`（`openjiuwen.core.single_agent.rail.base`）：

```python
class WriteSafetyRail:
    """write_file / import_zip 执行前的参数合法性闸门。失败即拒绝，不产生脏数据。"""
    async def on_tool_pre(self, event: AgentCallbackEvent):
        if event.tool_name not in ("write_file", "delete_file", "import_zip"):
            return
        problems = check_write_safety(event.tool_args)   # 路径穿越 / key 字符集 / key 重复
        if problems:
            raise ToolRejected(code="WRITE_UNSAFE", problems=problems,
                               next=["改用合法 key", "或换一个未占用的 key"])
```

用 Pre 而不是 Post：Post 发现问题时脏数据已经落盘。一致性校验**不放这里**，理由见功能地图 §3.8。

### 3.4 挂起原语的实现

`ask` 和 `request_confirmation` 是仅有的两个会挂起 run 的工具：

```python
async def invoke(self, tool_args, session=None) -> str:
    interaction_id = await self._core.create_interaction(tool_args)  # 落库
    await self._ctx.checkpoint(reason="ask", interaction=interaction_id)
    answer = await self._ctx.await_answer(interaction_id)   # 可能是几小时后
    return json.dumps(answer)
```

进程重启时不需要"记得"自己在等谁：待答交互在 core 的库里，恢复流程是「读未答交互 → 从对应 checkpoint 重建 session → 把答案作为工具返回值注入」。

### 3.5 团队编排：用 JiuwenSwarm，不自建 spawn

Swarm Skill 定义一支团队——有哪些角色、各自负责什么、如何协作、遇到问题怎么处理；SwarmFlow 是其中「可执行编排」的部分。Leader 的任务分解、Teammate 派发、结果汇总由框架承担，**我们不写 `spawn` 工具，也不管并发与汇总**。

我们只提供两样东西：工具面（通向 `modeling-core` 的唯一通道，不能交出去）和挂起原语（`ask` / `request_confirmation`）。

角色划分：

| 角色 | 任务 | 上下文 | 产出 |
| --- | --- | --- | --- |
| `material_analyst` | 读 1 份资料 | 干净 | `.modeling/analysis/<doc>.md` + 20 行摘要 |
| `knowledge_extractor` | 从 1 条已确认流程抽 operation/object/rule | 干净 + 该流程 | 候选修订 + 摘要 |
| `merge_analyst` | 分析来源包 1 个目录的冲突 | 干净 + 两侧定义 | 冲突条目列表 |
| `validator` | 独立复核一批产物 | 干净 | 问题清单 |

`validator` 必须是独立角色，**不能复用构建角色的自我结论**。v1 无校验器，这一角色的产出是给人看的问题清单，不构成阻断；真正的阻断来自沙箱测试和评测（见 [功能地图 §4](02-feature-map.md)）。

> **需要 spike 验证。** JiuwenSwarm 的 README 没有给出代码示例，以下三点必须先跑通再定稿：① Leader/Teammate 的工具面能否按角色差异化注入；② Swarm 的 HITL（`human` / `human_session`）能否承载我们的 `ask` / `request_confirmation` 挂起语义，还是要退回到自己在工具里挂起；③ Cluster 模式下的 checkpoint 粒度。若 ①② 任一不成立，退化方案是用 `ReActAgent` + 一个薄 `spawn` 工具，但那是退化，不是首选。

### 3.6 从同仓服务验证过的工程约束

这些问题可参考 `consumer/backend` 和其他建模 Agent 的经验，但新运行时独立实现，不桥接旧运行时，也不复制其编排代码：

| 坑 | 处理 |
| --- | --- |
| `Runner` 持有进程级单例（`callback_framework` 等） | 复用 consumer 的 `_JiuwenLoopRunner` 模式：专用 loop 线程 + 显式生命周期 |
| 临时 agent 泄漏 callback / checkpoint 状态 | 每个 ephemeral agent 用完显式释放（consumer 有对应清理代码和测试） |
| openJiuwen 自己的日志会写到 `logs/logs/` 嵌套目录 | 启动时重定向 openJiuwen 日志路径 |
| OpenAI-compatible 端点的 reasoning 字段别名不一致 | 保留 consumer 的 `_patch_jiuwen_openai_reasoning_alias` 补丁 |
| `openjiuwen==0.1.14` 约束 FastMCP `<3.0` | 升级 FastMCP 前必须先升 openjiuwen |
| 冷启动慢 | 复用 `warm_up_jiuwen_runtime`：进程启动时预热模块和事件循环 |
| API 在小版本间可能变动 | 沿用 consumer 的 `importlib` + `getattr` 探测式适配层，不直接硬绑内部类 |

---

## 4. 数据模型

### 4.1 仓库（事实源）

```
DATA_DIR/artifacts/<project_id>/          isomorphic-git 仓库
  manifest.yaml  index.md
  diagrams/  objects/  operations/  rules/  metrics/  scripts/  mappings/
  .modeling/
    goal.md              建模目标，确认后绑定 revision
    plan.md              当前计划，agent 维护，前端渲染
    analysis/<doc>.md    子 agent 材料分析产物
    decisions/<n>.md     确认点：问题、答案、影响文件、确认人、角色、时间
    provenance.yaml      融合来源：pkg:<name>@<version> → 条目列表
```

### 4.2 SQLite（git 存不下的）

| 表 | 用途 | 变化 |
| --- | --- | --- |
| `projects` `users` `documents` `data_dict` | 不变 | — |
| `artifact_revisions` `package_versions` | 不变 | — |
| `confirmations` | revision + 用户 + 角色 + 时间 + change_group | 强化 |
| `project_agent_state` | 每项目唯一当前状态；单个 `state` 值统一阶段与执行状态 | **Agent 运行时新表** |
| `project_agent_state_transitions` | 状态转换追加审计，不参与当前状态判断 | **Agent 运行时新表** |
| `agent_interactions` | `ask` 与 `request_confirmation` 的事实记录；当前待答 ID 仍以唯一状态行为准 | **Agent 运行时新表** |
| `agent_runs` | run 输入、session/checkpoint 标识和最终结果记录；不承载项目当前阶段 | **Agent 运行时新表** |
| `agent_events` | 消息与工具调用轨迹（脱敏后） | **Agent 运行时新表** |
| `agent_candidate_workspaces` `agent_candidate_changes` | 未确认的项目候选覆盖层；新 run 显式接续 | **core 新表** |
| `evidence_index` | 知识条目 → 证据来源，可从文件重建 | 强化 |
| `confirm_policy` | 项目级确认策略偏好 | **新** |
| `eval_*` `failure_diagnoses` `optimization_*` | 不变 | — |
| ~~`ai_construction_graphs` / `ai_construction_tasks`~~ | 删除 | 计划变成文件 |
| ~~`authoring_runs`~~ | 删除 | 由 agent 运行时接管 |

### 4.3 检查点与修订的绑定

我们只记录 `(checkpoint_id, session_id, baseline_rev, candidate_rev, reason, interaction_id?)`——即框架 checkpoint 与知识包状态的**绑定关系**，不含 messages 或 agent 内部状态（那正是现在 `authoring_runs` 的做法，要删掉）。这让三件事同时成立：进程恢复、用户回退、审计追溯。回退到某检查点 = 从其 `baseline_rev` 建新修订 + 从该 checkpoint 重建 session，历史不删。

---

## 5. 前端协议

事件流**不自建**：桥接 openJiuwen 的 `AgentCallbackEvent` 与 `write_invoke_result_to_stream`，consumer 的 `JiuwenModelTraceBridge` 已有可抄的实现。我们只做一层转换——把框架事件映射成下面的判别式类型，并在这一层脱敏。


| 通道 | 用途 |
| --- | --- |
| `GET /api/projects/:id/stream`（SSE，agent 服务） | 消息、状态、focus 指令、工具进度、挂起通知 |
| `POST /api/interactions/:id/answer`（agent 服务） | 应答 `ask` / `request_confirmation` |
| `POST /api/projects/:id/steer`（agent 服务） | 用户插话，下一个工具边界注入 |
| `GET /api/projects/:id/tree|file|diff`（core） | 只读、可缓存、可并发 |
| `GET /api/projects/:id/gates`（core） | 门禁面板，每次实时重算 |

事件类型收敛为一套判别式 schema：

```ts
type AgentEvent =
  | { kind:'message'; role:'assistant'; blocks: Block[] }
  | { kind:'focus'; target: FocusTarget }
  | { kind:'interaction'; id: string; ask: AskPayload }
  | { kind:'progress'; text: string; counters: Counters }
  | { kind:'tool'; name: string; summary: string }   // 只进过程抽屉
  | { kind:'checkpoint'; id: string; revision: string }
```

`Block` 由服务端校验，模型不能输出任意 HTML 或组件名。这一条从现有设计原样保留。

---

## 6. 实施分期

假设 1 名后端 + 1 名前端全职，模型能力按当前水平。

| 阶段 | 内容 | 周 | 验收 |
| --- | --- | --- | --- |
| **P0 工具化核心** | 把 24 个工具暴露成 `modeling-core` 的内部 HTTP API；新增 `gates.ts`、`references.ts`；**删除全部校验器与 `submit_*` 遗留，5 条写入安全内联进 `write_file`/`import_zip`** | 2 | 24 个端点全部可 curl 调通；5 条写入安全各有拒绝用例；旧系统零影响 |
| **P1 运行时骨架 + Swarm spike** | Python `modeling-agent` 服务：工具桥、session/checkpoint、事件流桥接、挂起恢复；**先做 §3.5 的三点 spike 决定 Swarm 还是 ReActAgent** | 4 | spike 结论落纸；空项目上跑通「读资料 → 写 goal.md → ask → 杀进程 → 恢复 → 收到答案继续」 |
| **P2 建模闭环** | Leader prompt、子 agent 角色、完整建模路径到「建模完成确认」 | 4 | 用现有真实资料集跑完，产出的包 **consumer 导入成功且评测通过率不低于旧系统**；与旧系统产出做结构对比，差异可解释 |
| **P3 前端新壳** | 三栏 + 单一对话流 + `focus` + 确认卡 + diff + 门禁面板 + 过程抽屉 | 3 | 旅程 A 端到端可操作 |
| **P4 增量与融合** | `who_references` 强化、融合集合运算、选择性导入、provenance | 3 | 旅程 B、C 端到端 |
| **P5 收尾** | 删旧编排器、历史项目投影迁移、审计包导出、双跑期结束 | 2 | 旧代码删除，测试套件重整完成 |

合计约 **18 周**。P0 完成后即可并行推进 P1 与 P3 的设计。Swarm spike 是 P1 的第一件事，它的结论会影响 P2 的角色设计。

### 切换策略

新工作台直接调用新的 Agent 运行时，不建立旧运行时兼容桥。历史项目如需进入新工作台，只从已有 Git 修订、确认和评测事实初始化 `project_agent_state`；缺证据的门禁保持待补充，不伪造当前状态。

---

## 7. 测试策略

这是最容易被低估的一笔。现有 203 个测试文件、38k 行里，大部分测的是编排器状态迁移，会随编排器一起失效。

| 类别 | 处置 |
| --- | --- |
| 门禁谓词、包渲染、mermaid 解析、评测管道的测试 | **原样保留**，它们测的是不变量 |
| 编排器状态迁移、任务图调整、里程碑回退的测试 | 随代码删除 |
| 校验规则测试 | **随校验器一起删除**（v1 全删校验器）。后续按判据加回检查时，每条新检查配一个"输入包 → 期望 finding"的固化用例 |
| 写入安全测试 | **新增**：5 条各有一个拒绝用例 + 一个放行用例。这是 v1 唯一的静态断言 |
| 工具契约测试 | **新增**：每个工具的 schema、错误分支、脱敏行为 |
| 挂起/恢复测试 | **新增**：杀进程后从 checkpoint 续跑，答案正确注入 |
| 端到端建模 eval | **新增，且是 v1 的唯一质量判据**：固定资料集 + `temperature=0`，断言 consumer 导入成功、产物数量落在区间、关键规则存在、评测通过率 ≥ 阈值 |

端到端 eval 慢且非确定，放单独 CI job，用**区间断言**而不是精确断言。**改 prompt 必须跑这个套件**——prompt 现在是承载建模方法论的地方，等同于业务代码。

删掉校验器后，静态断言从约 30 条降到 5 条，质量保证的重心整体移到"真的跑起来"：沙箱测试、consumer 导入、评测通过率。这是有意的取舍——代理指标换目标指标。

---

## 8. 配置

```bash
# modeling-agent/.env.example
MODELING_CORE_URL=http://localhost:5100        # 确定性核心
MODELING_AGENT_PORT=5110
MODELING_LLM_BASE_URL=                          # OpenAI-compatible
MODELING_LLM_MODEL=
MODELING_LLM_API_KEY=
MODELING_AGENT_MAX_ITERATIONS=200               # 取代旧的 MAX_TURNS=12
MODELING_AGENT_CONTEXT_ROUNDS=40
MODELING_AGENT_TOKEN_BUDGET_PER_PROJECT=        # 成本闸门，超出挂起并告知用户
JIUWENBOX_API_URL=                              # 脚本沙箱，与 consumer 同源
CORE_SERVICE_TOKEN=                             # 仅 agent→core 内部调用，≥32 随机字符
```

验收必须核对实际 run 审计中的模型 ID 与 `MODELING_LLM_MODEL` 一致，禁止无提示回落。当前 15100 验收环境使用 `deepseek-v4-flash`；切换到 `glm-5.2` 时也必须先通过真实工具调用验收。

compose 新增 `modeling-agent` 服务，与 `modeling` 同网络、共享 `modeling-data` 卷的**只读**挂载（写入一律走 API）。

---

## 9. 风险登记

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| agent 长循环跑偏、绕圈 | 烧 token、产出垃圾 | 无进展保护（连续 N 轮无写入 / 无新证据则中止报告） |
| 上下文超限导致"失忆" | 重复提问、前后矛盾 | 子 agent 承担重活 + `project_agent_state` 精简摘要 + 每个 run 独立 session + ContextEngine window |
| 成本与时延上升 | 单项目成本可能翻数倍 | 项目级 token 预算闸门；便宜模型跑子 agent，主循环用强模型 |
| openJiuwen 小版本 API 变动 | 运行时崩 | 探测式适配层（抄 consumer）；版本锁定 + 升级前跑 eval 套件 |
| 提示词漂移 | 行为悄悄变差 | prompts 入版本控制；改 prompt 强制跑 eval 套件；记录每次 run 的 prompt 版本 |
| 非确定性与可审计的张力 | 客户审计质疑 | 门禁确定、确认记录绑定修订、每次写入产生修订、全轨迹可导出 |
| **v1 无校验器** | 结构问题可能拖到 consumer 导入或评测才暴露 | 有意的取舍：代理指标换目标指标。缓解＝沙箱测试 + consumer 导入 + 全量用例通过三道真实闸门；每次真实失败按 [功能地图 §2.3](02-feature-map.md) 的判据补一条检查并记录来源 |
| 检查加回后规则集再度膨胀 | 重演当前的"卡住"问题 | 每条加回的检查必须在决策记录里写明对应哪次真实失败；`code → severity/disposition` 做成配置表，调严格程度不改代码 |
| 9 步流程是交付承诺 | 改架构影响合同口径 | 9 步保留为从产物投影的只读视图，不作为引擎 |
| JiuwenSwarm 的 HITL 与工具注入语义未验证 | 团队编排方案可能不成立 | P1 先做 spike（§3.5 三点）；退化方案是 `ReActAgent` + 薄 `spawn` 工具，代价可控 |
| "去硬码"过头，把不变量也去掉 | 静默数据丢失、越权写入 | [功能地图 §9](02-feature-map.md) 给出硬码下界清单；进程隔离作为执行机制 |
| 双语言双进程 | 运维复杂度 | 统一 trace id 贯穿两个服务；两个服务同生命周期部署；§2 给出了合并条件 |
| 现有测试大面积失效 | 安全网空窗 | 先保住不变量测试（P0 即完成），再动编排器（P5） |

---

## 10. 第一步

如果只做一件事：**给 agent 开 `write_file`，把 5 条写入安全内联进去，然后删掉 `validateSpecIntegrity` 的 throw。**

`spec-validation.ts` 末尾那行 `if (errors.length) throw` 是当前建模卡顿的根——任何一条检查失败就整批生成作废。删掉它、删掉整个校验层、只留写入安全，编排器立刻失去存在理由，其余部分会自己塌下来。
