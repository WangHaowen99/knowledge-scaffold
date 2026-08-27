# 功能地图 · 纯 agent 建模工作台

> 状态：设计草案 · 2026-08-22
> 配套：[前端草图](01-frontend-mockup.html) · [用户旅程](03-user-journeys.md) · [技术选型与实施](04-tech-stack-and-implementation.md) · [后端设计](05-backend-design.md)
>
> 本文回答「有什么」。运行时怎么跑、工具前置条件全表、流程控制的三层承载见[后端设计](05-backend-design.md)。

## 0. 一句话

建模不是一条工作流，是一个 agent 在受约束的沙盒里把资料变成知识包仓库。系统只负责三件事：**给 agent 能力、给 agent 边界、给人类否决权**。

---

## 1. 分层与职责

```
┌─────────────────────────────────────────────────────────────┐
│ 前端工作台                                                    │
│   产物视图（目录树 / 画布 / diff）· 对话流 · 门禁面板 · 过程抽屉  │
└──────────────┬──────────────────────────┬───────────────────┘
               │ 事件流 + 应答             │ 只读 GET（文件/树/diff）
┌──────────────▼──────────────┐  ┌────────▼────────────────────┐
│ Agent 运行时                  │  │ 确定性核心                    │
│ Leader ReAct 循环             │  │ 工件仓库（git，修订不可变）      │
│ 子 agent 并行（资料/抽取/融合） │→ │ 写入安全（路径/key 清洗）        │
│ 会话 · 检查点 · 上下文压缩      │  │ 门禁谓词                       │
│ 工具调用与审批                 │  │ 引用查询原语 who_references      │
└──────────────────────────────┘  │ 评测管道 · consumer 客户端       │
                                  │ 文档解析 · 数据字典               │
                                  │ 认证 · 单写者租约                 │
                                  └─────────────────────────────────┘
```

分界线只有一条：**失败是静默的、不可逆的、或者会让模型给自己打分的，归确定性核心；失败是可见的、专家能当场纠正的，归 agent。**

---

## 2. 现有模块处置

### 2.1 保留（改造成工具后端，不重写）

| 现有模块 | 现有行数 | 保留后 | 新角色 |
| --- | --- | --- | --- |
| `artifacts/package-store.ts` | 2283 | ~1000 | 只留 git 读写、候选修订、变更组依赖图、原子提交、租约；包骨架模板与 spec 版本构建随 `submit_*` 一起删 |
| `artifacts/isomorphic-git-repository.ts` | 572 | 572 | 修订存储，不变 |
| `workflow/keys.ts` 的写入安全部分 | ~40 | ~40 | 内联进 `write_file`/`import_zip`，见 §3.7 |
| `workflow/package-import.ts` | 445 | ~300 | 解压 + provenance；不再解析成 spec |
| `documents/parser.ts` | 328 | 328 | xlsx / docx / pdf 解析 |
| `flowchart/*` | 1049 | **~100** | 只留「提取节点/边 ID」供 operation 关联；`.mmd` 是文件，agent 直接读写，渲染交前端 mermaid.js |
| `evaluation/engine.ts` | 418 | 418 | 评测执行管道（judge 走 openJiuwen） |
| `consumer/*` 客户端 | — | — | import-zip / data-source / runs·sync |
| `auth/*`、`settings/*`、`db/*` | — | — | 不变（`settings` 只存配置，不再构造 LLM 客户端） |
| `graph-import/*` | 784 | 784 | 数据源绑定，不变 |
| `core/gates.ts` | 新增 | ~80 | v1 门禁只查确认记录、候选状态、评测结果 |

合计从约 7–9k 进一步收到 **约 4.5–5k**。校验器不在其中——v1 全部删除，见 §2.3。

**收缩的两个来源：**「submit_* 时代的遗留」和「openJiuwen 已经提供的」，分别见 §2.3 与 §2.4。

### 2.2 删除

| 现有模块 | 行数 | 删除理由 |
| --- | --- | --- |
| `ai-construction/orchestrator.ts` | 3137 | 编排职责交给 agent 循环 |
| `ai-construction/task-graph.ts` | 1340 | 计划变成 `.modeling/plan.md` 文件 |
| `ai-construction/task-editing.ts` | 490 | 用户直接跟 agent 说，不需要任务图编辑 API |
| `ai-construction/milestone-rollback.ts` | 321 | 回退 = 从确认点建新修订 |
| `ai-construction/messages.ts` | 697 | 消息块类型收敛为一套 `kind` 判别 schema |
| `ai-construction/controlled-repair.ts` | 1722 | v1 无校验器即无「受控修复」；后续修复由 agent 读工具返回值自行迭代 |
| `workflow/m2.ts` 分阶段编排 | 1235 | 阶段推进由 agent 决定 |
| `workflow/material-analysis-checkpoints.ts` | 391 | 检查点交给 openJiuwen session checkpoint |
| `authoring/state.ts` + `authoring/routes.ts` 的任务部分 | ~2500 | run 状态由 agent 运行时持有 |

合计约 12k 行编排代码删除，另有约 8k 行路由与前端胶水随之退休。

### 2.3 v1 删除全部校验器与 submit_* 遗留

| 现有模块 | 行数 | 说明 |
| --- | --- | --- |
| `workflow/spec-validation.ts` | 398 | 仅 script entrypoint 路径检查移入写入安全 |
| `workflow/package-check.ts` | 605 | 规则层与渲染产物层全删 |
| `workflow/knowledge-evidence.ts` | 256 | evidence 仍记录并展示，v1 不强制非空 |
| `workflow/object-alias-validation.ts` | 122 | 别名冲突在融合场景由冲突检测器覆盖 |
| `workflow/data-mapping.ts` | — | 数据映射问题由数据源绑定阶段的真实连接测试暴露 |
| `workflow/script-safety.ts` | 93 | JiuwenBox 沙箱才是安全边界，静态检查是纵深防御 |
| `keys.ts` 的一致性检查部分 | ~60 | 仅保留写入安全的 5 条 |

**为什么 v1 全删。** 现有约 30 条检查是对着编排器的产出调出来的，agent 驱动的产出会有不同的失败模式；先用旧尺子量新东西，得到的只是噪音。更重要的是：判断包对不对的终极标准是 **consumer 能否导入 + 评测通过率**，不是能否过校验器。校验器是代理指标，评测是目标。

**加回的判据只有两个，都必须是真实失败数据：**

1. consumer 导入失败或运行期出错 → 补一条结构检查
2. 评测用例失败，且失败诊断归因到**包结构问题**（不是业务口径问题）→ 补一条

不凭"看起来应该检查"加回去。每加回一条，在决策记录里写明它对应哪次真实失败——这条记录本身会阻止规则集再次膨胀。

#### submit_* 时代的遗留

这些模块的存在前提是「模型提交结构化 payload，服务端渲染成文件」。agent 直接 `write_file` 写 markdown 之后，它们**没有输入了**：

| 模块 | 行数 | 说明 |
| --- | --- | --- |
| `workflow/package-render.ts` | 386 | `PackageSpec → 标准包文件集`。agent 直接写文件，渲染器失去输入 |
| `workflow/package-draft-parser.ts` | 540 | 反向解析文件成 spec，同理 |
| `workflow/tools.ts` 的 17 个 `submit_*` schema | ~350 | 表单契约；工具参数 schema 另写，量小得多 |
| `package-store.initializeDraftPackage` | ~70 | 硬码的包骨架模板。改为「建一个空 git 仓库」，骨架由 agent 按 Skill 生成 |
| `flowchart` 的 Mermaid 解析与派生 | ~950 | 结构化文档 → 派生 `.mmd` 的单向管线。新设计里 `.mmd` 就是事实源 |
| `src/error-next-actions.ts` | 155 | 「错误 → 下一步动作」的硬码映射。这类建议属于 Skill 文本，不属于代码 |

### 2.4 openJiuwen 已经提供的，不要自建

后端设计理念是**尽量基于 openJiuwen agent、去掉硬码**。按这条尺子，以下都不该由我们实现：

| 现有/拟建自建实现 | 行数 | 换成 |
| --- | --- | --- |
| `llm/provider.ts`（`buildLlm` / `buildTaskLlm` / provider 映射 / 错误归一化） | 425 | `ModelClientConfig` + `ModelRequestConfig`；provider 映射抄 consumer 的 `_map_jiuwen_provider` |
| `workflow/agent.ts`（pi 封装、`MAX_TURNS`、超时、重试） | ~200 | `ReActAgent` + `ReActAgentConfig.max_iterations` |
| `makeAgent` 的 `transformContext` 消息裁剪 | — | `ContextEngine` / `ContextEngineConfig`。**绝不在工具桥或循环里自己裁消息** |
| `authoring_runs` 序列化 `messages` 数组 | — | `create_agent_session` + `CheckpointerFactory`。我们只存 `checkpoint_id ↔ 知识包修订` 的绑定 |
| 自建 `spawn` 工具 + 并发/汇总管理 | — | **JiuwenSwarm Leader/Teammates**（见 §3.5） |
| 自建确认策略引擎 `core/policy.ts` | — | **JiuwenSwarm tool permission**（见 §5.1） |
| 自建事件发布层 | — | `AgentCallbackEvent` + `write_invoke_result_to_stream`，桥接即可 |
| 自建脚本沙箱 | — | **JiuwenBox**（consumer 已在用） |

判据：**凡是"agent 怎么跑"的问题，交给 openJiuwen；只有"知识包是什么、什么绝对不允许"才是我们的代码。**

### 2.5 新增

| 新模块 | 规模 | 职责 |
| --- | --- | --- |
| `agent/tools/*` | ~600 | 工具实现与 schema，agent 的全部能力面（薄，每个工具是一次 HTTP 转发 + 事件发布） |
| `agent/skills/*` | 文本 | Swarm Skill：团队角色、建模方法论、各会话类型的操作手册 |
| `agent/rails.py` | ~80 | 写入安全 Pre-hook + 工具审批策略映射 |
| `agent/bridge.py` | ~200 | openJiuwen 事件 → 前端事件流的转换与脱敏 |
| `core/gates.ts` | ~80 | 门禁谓词 |
| `core/references.ts` | ~60 | `who_references` 一跳引用查询原语 |
| `core/merge/*` | ~60 | 融合的集合运算（key 撞 / 别名撞 / 引用断 / mapping 指向） |

**没有 `agent/loop.py`**——Leader 循环、检查点、挂起恢复由 openJiuwen 提供。
**没有 `core/policy.ts`**——确认策略是一张映射到 JiuwenSwarm tool permission 的配置表，不是引擎。
**没有 `core/evidence-index.ts`**——不维护索引表，`who_references` 是纯计算（从文件里 grep evidence 引用），失效传播由 agent 逐跳展开。

---

## 3. Agent 工具面（完整契约）

这是 agent 能做的全部事情。工具之外的世界对它不存在。

### 3.1 资料（只读）

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| `list_documents` | `() → [{filename, kind, revision, size}]` | 含资料修订号 |
| `read_document` | `(filename, offset?, limit?) → text` | 分页精读 |
| `search_documents` | `(query) → [{filename, offset, excerpt}]` | 空格分隔 OR |
| `list_tables` | `() → [{table, columns[]}]` | Excel 数据字典 |
| `describe_table` | `(table) → {columns, types, samples, uniqueness}` | |
| `read_table_rows` | `(table, offset, limit≤50) → rows` | |

### 3.2 知识包（读写，写入即候选修订）

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| `list_dir` | `(path) → entries` | |
| `read_file` | `(path) → text` | |
| `write_file` | `(path, content, change_group, evidence[]) → {candidate_rev}` | evidence 记录并在前端展示；v1 不强制非空（见 §2.3） |
| `delete_file` | `(path, change_group, reason)` | |
| `grep_package` | `(pattern) → hits` | 跨文件检索，融合和影响分析都用 |

写入永远落在候选修订上，不动当前已确认修订。`change_group` 是确认的最小单位。

**所有工具返回值统一带 `display` 字段**（一句人话 + 可选进度），供对话流底部的活动行使用——前端不解析工具名拼文案。见[后端设计 §7.1](05-backend-design.md)。

### 3.3 确定性事实（agent 无法伪造返回值）

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| ~~`validate`~~ | `(scope) → {errors[], warnings[]}` | **v1 不提供**（校验器全删）。后续加回时按 §3.8 的位置分工 |
| `gate_status` | `(gate) → {pass, blockers[]}` | |
| `who_references` | `(target) → {referrers[], via[]}` | **一跳**引用查询原语。传播多远由 agent 决定，不硬码层数 |
| `diff` | `(rev_a, rev_b, scope?) → structured diff` | |

### 3.4 人机

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| `ask` | `(question, kind, options?, evidence?) → answer` | **挂起 run**。`kind` ∈ 澄清 / 业务裁决 / 冲突裁决 / 范围确认 / **资料请求**（`material_request`，必带「我口述」出口） |
| `request_confirmation` | `(change_groups[], summary) → {approved[], rejected[], review_mode}` | 挂起。唯一能把候选变成已确认的路径。每组必带 `tier`，组数 ≤ 5（见[后端设计 §7.2](05-backend-design.md)） |
| `focus` | `(target)` | 只影响前端中栏，不改状态 |
| `notify` | `(text, level)` | 进度说明，不挂起 |

**只有两个挂起原语。** 现有 8 类待确认事项全部变成 `ask` 的 `kind` 差异，由一套判别式 schema 校验、前端按 kind 渲染。

### 3.5 计划与委派

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| `get_project_state` | `() → {state, revision, summary, open_questions, allowed_transitions}` | 从 Agent 运行时读取项目唯一状态和经过门禁校验的下一步 |
| `transition_project_state` | `(to_phase, expected_revision, intent, summary, evidence, open_questions)` | CAS 跳转；Agent 运行时校验合法边，并调用 core 的确定性门禁 |
| `update_project_context` | `(expected_revision, summary, open_questions, evidence)` | 阶段不变时更新同一状态行，不建立 Markdown 状态副本 |
| `update_plan` | `(markdown)` | 写 `.modeling/plan.md` 的工作拆解；不得声明当前阶段或门禁结果 |
| ~~`spawn`~~ | — | **不自建。** 子 agent 由 JiuwenSwarm 的 Leader/Teammates 承担，角色与协作规范写在 Swarm Skill 里，Leader 的分解与交接由框架负责 |

Swarm Skill 定义团队（有哪些角色、各自负责什么、如何协作、遇到问题怎么处理），我们只提供工具面和挂起原语——工具是通向 `modeling-core` 的唯一通道，不能交出去。角色定义见 [技术选型 §3.5](04-tech-stack-and-implementation.md)。

### 3.6 执行

| 工具 | 签名 | 说明 |
| --- | --- | --- |
| `run_script_test` | `(script, cases) → results` | JiuwenBox 沙箱 |
| `run_evaluation` | `(case_ids?) → {passed, failed, traces}` | 走现有评测管道 |
| `query_eval_db` | `(sql) → rows` | 只读，数据探查用 |

**共 24 个工具，v1 实装 23 个**（`validate` 不提供，`spawn` 交给 Swarm）。对比现状：6 个只读检索 + 17 个 `submit_*` 表单。数量相当，语义完全不同——这些是能力，那些是表单。

### 3.7 写入安全（Pre-hook，不是校验器）

v1 删除全部校验器，但以下 5 条跟着 `write_file` / `import_zip` 走。它们不是校验，是写入函数自己的参数合法性检查，共同点是**失败静默且不可逆**：

| 检查 | 现有位置 | 后果 |
| --- | --- | --- |
| key 含 `..` | `keys.ts:32` | 路径穿越 |
| key 含 `/ \ # ? & \|` 或控制字符 | `keys.ts:33-34` | 路径穿越、文件名破坏 |
| 同类资源 key 重复 | `keys.ts:41` | **详情文件被静默覆盖**（静默丢数据） |
| zip 条目路径含绝对路径 / 反斜杠 / `..` / 空段 | `keys.ts:90-96` | 解压穿越 |
| script entrypoint 非 `.py` 或含 `..` | `spec-validation.ts` | 路径穿越 |

以 **PreToolUse hook** 实现（openJiuwen `AgentCallbackEvent`，`single_agent.rail.base`），失败直接拒绝写入并返回可执行错误。用 Pre 而不是 Post：Post 发现问题时脏数据已落盘。

### 3.8 校验的三个位置

即使后续加回检查，也要按位置分工，不能全塞进 hook：

| 位置 | 跑什么 | 失败行为 | 理由 |
| --- | --- | --- | --- |
| **Pre-hook** | 单次写入的路径 / key 合法性 | 拒绝写入 | 无跨文件依赖、无中间态，失败即真失败 |
| **工具 `validate`** | 引用完整性等一致性检查 | 返回结果 | 天然跨文件、天然有中间态，时机与 scope 必须由 agent 定 |
| **门禁** | 状态跃迁的前置条件 | 阻断跃迁 | agent 可以一直不调 validate，但想让用户确认时必须过 |

**「agent 会不会忘记校验」的答案是门禁，不是 hook。** 全放 PostToolUse hook 有两个后果：agent 先写 operation 再写它引用的 rule 是合理路径，中间必然出现悬空引用，hook 会在每一步尖叫；一次建模几百次写入，每次注入校验结果会把上下文刷爆。

---

## 4. 门禁

门禁是纯谓词，不推进任何东西，只回答能不能过。agent 什么时候撞、撞几次、撞不过怎么办，都是它自己的事。

**v1 门禁只依赖三类真实事实：确认记录、候选状态、真实执行结果（沙箱测试 / 评测）。不依赖任何校验器。**

| 门禁 | 谓词（v1） |
| --- | --- |
| 资料就绪 | 至少 1 份资料解析成功，且无解析失败未处理 |
| 建模目标确认 | `.modeling/goal.md` 存在且有绑定 revision 的确认记录 |
| 业务流程确认 | 每条 `diagrams/*.mmd` 有确认记录 |
| 知识结构确认 | 知识结构有确认记录 |
| 确定性能力确认 | 每个 script 有输入输出契约、**沙箱测试通过**、绑定 operation |
| 建模完成确认 | 无待确认候选 且 无未答 `ask` |
| 进入验证 | 建模完成确认 |
| 正式发布 | 全部用例执行完成且 pass ∧ 无待确认候选 ∧ 版本号已确认 |

「确定性能力确认」的沙箱测试和「正式发布」的全量用例通过，是 v1 仅有的两道真实质量闸门。它们不是校验器——是真的把代码跑起来、把问题问一遍。这比过一遍静态规则更接近"包能不能用"。

后续按 §2.3 的判据加回检查时，只把**确认过会导致 consumer 消费失败**的那些挂到门禁上，其余留在 `validate` 工具里供 agent 自查。

门禁**顺序是硬的**，通往门禁的路径是软的。这是这套设计里唯一不让 agent 碰的流程结构。

---

## 5. 确认与权限

### 5.1 确认策略（两个粒度，两个归属）

之前把确认策略整体归给 JiuwenSwarm tool permission，那一处过头了。两者管的不是同一个粒度：

| 粒度 | 归属 |
| --- | --- |
| 工具级：这个工具要不要审批 | JiuwenSwarm tool permission |
| **变更组级：这个工具的这部分参数要不要审批** | **core 的 `request_confirmation` 实现**（约 30 行） |

Swarm 不知道「变更组」是什么，也不该知道。完整说明见[后端设计 §7.4](05-backend-design.md)。

变更组级策略是项目级用户偏好，前端常驻在确认卡下方（显式开关，不是隐藏偏好），默认值保守：

| 变更类别 | 默认 | 可设为 |
| --- | --- | --- |
| 结构性修复（索引同步、格式、引用路径） | 询问 | 自动 |
| 知识语义修改（对象、规则、operation、mapping） | 询问 | 始终询问（不可设为自动） |
| 业务口径裁决（阈值、分支、冲突） | 询问 | 始终询问（不可设为自动） |
| 脚本生成与绑定 | 询问 | 始终询问（不可设为自动） |

只有第一类允许降级为自动。后三类的"始终询问"是硬约束，写在核心里而不是策略里——前端把它们显示为「必须问 / 不可关闭」。策略变更**不回溯**已生效的确认记录。

v1 无校验器，因此第一类（结构性修复）暂时不会产生——它原本的触发源就是校验器的可自动修复项。类别定义先保留，等 §2.3 的判据把检查加回来时直接生效。

**Swarm 默认值必须改。** 默认"每个工具都要审批"在建模场景是灾难——一次建模几百次工具调用。分级见 [技术选型 §3.3](04-tech-stack-and-implementation.md)：只读免审批，`write_file` 免审批（写的是候选修订，可整体退回），只有 `request_confirmation` 和发布必须人。**审批点从"每次写"收敛到"确认候选"，这是让审批可承受的关键。**

### 5.2 角色

| 角色 | 可确认 |
| --- | --- |
| 项目负责人 | 全部，含发布、用例覆盖确认、版本号 |
| 建模人员 | 结构与知识结构确认 |
| 领域专家 | 业务口径裁决、冲突裁决 |
| 验证人员 | 用例改判 |
| 只读成员 | 无 |

首版可以让项目负责人默认持有全部角色，但确认记录从第一天就存实际角色。

---

## 6. 增量建模

新资料进来后**不重跑**，只算差量。

```
新增/替换资料
  → 解析 + 语义差异判定（agent）
  → who_references(资料修订) 查一跳引用（确定性原语）
  → agent 用 who_references 逐跳展开，自己判断传播边界，标记「过时」
  → 未命中的内容显式列为「保持有效」
  → 冲突逐条裁决（ask，人工）
  → 生成候选修订（write_file）
  → 定向重评（只跑引用了受影响内容的用例）
  → request_confirmation → 新已确认修订
```

| 能力 | 归属 | 说明 |
| --- | --- | --- |
| 语义差异 vs 格式差异 | agent | 排版、错别字不传播失效 |
| 影响范围计算 | 确定性原语 + agent 决策 | 工具只提供 `who_references(target)` 这一个原语（一跳引用查询）；**传播多远由 agent 决定**，不硬码「传播一层」这种启发式 |
| 「保持有效」清单 | 确定性 | **必须显式呈现**，这是用户信任增量的前提 |
| 冲突裁决 | 人工 | 三选项：采用新 / 保留旧（留痕）/ 分段共存 |
| 定向重评 | 确定性 | 只跑引用了受影响内容的用例 |

已发布版本在用户确认新修订前保持可用。

## 7. 知识包融合

融合复用增量的全部机制，差量来源从"资料"换成"另一个知识包"。

```
导入来源包（可选择性导入子集）
  → 解析为结构化条目 + provenance 标记
  → 冲突检测（确定性）
  → 自动合并可合并项 / 冲突逐条裁决（人工）
  → 生成融合候选修订
  → 融合引用解析预演（融合自带，非通用校验器）→ 裁决后重算
  → request_confirmation
```

### 7.1 冲突检测：确定性只做集合运算，语义判断交给 agent

**确定性部分（`core/merge/*`，约 60 行集合运算）：**

| 类型 | 检测方式 |
| --- | --- |
| key 撞车 | 两侧 key 集合求交 |
| 别名撞车 | 同 alias 指向不同 key |
| 引用断裂 | 来源包引用了未勾选导入的条目 |
| mapping 指向不同数据源 | 同 object 绑定不同 Sheet |

**交给 `merge_analyst` 子 agent 的（原本被误列为确定性）：**

| 类型 | 为什么不能硬码 |
| --- | --- |
| 定义可并 vs 互斥 | 要读懂两条规则的结论是否相反，这是语义判断 |
| operation 重叠 | 「步骤序列高度相似」需要相似度算法——典型的硬码启发式，阈值调不准 |
| 指标口径是否真的不同 | 同 grain 也可能口径不同，不同 grain 也可能等价 |

agent 拿到 key 撞车清单后逐条读两侧定义并给出分类与建议，用户裁决。**不写相似度算法。**

### 7.2 关键设计约束

- **选择性导入前置。** 先缩范围再谈冲突。把 5 个冲突降到 2 个的操作比裁决 5 次更有价值。
- **provenance 必须保留**：`provenance: pkg:<name>@<version>`，来源包升级时能再算一次增量。
- **命名空间前缀是有代价的选择**，界面要说出代价（近义对象让检索命中错误），默认不加前缀、仅冲突时重命名。
- **来源包不被修改**，融合是当前包的一次候选修订，可整体退回。

---

## 8. 前端功能地图

| 区域 | 组件 | 后端能力 |
| --- | --- | --- |
| 顶栏 | 目标 / 修订 / 待确认数 / 运行状态 / 产物计数 | 事件流 + `gate_status` |
| 左栏 | 两个 tab：**知识包**（目录树 / 影响热图 / 变更范围）· **资料**（列表 + 引用计数） | 工件仓库 GET · `who_references` · 资料反查端点 |
| 中栏 | 渲染 / 源码 / diff / 冲突表 / 影响分析 / 门禁面板 | `diff`、`who_references`、`gate_status` |
| 右栏 | 单一对话流 + 底部**活动行**（三级递进）· 确认卡分诊 · 常设策略开关 | 事件流 · `ask` · `request_confirmation` · `display` 字段 |
| 全屏 | 仅项目创建时的上传页；之后补资料一律在对话流内 | 上传接口 · `ask(kind='material_request')` |
| 抽屉 | 执行单元树 · 工具轨迹 · 检查点 | agent 运行时事件日志 |

### 8.1 退休的前端概念

| 现有 | 去向 |
| --- | --- |
| `WorkflowTimeline`（9 步导航） | 降级为顶栏状态条；9 步若是交付承诺，作为从产物投影出的只读视图 |
| `PendingInteractionQueue` | 内联到对话流；顶栏计数点开时以浮层出现 |
| `TaskGraphEditDialog` / `TaskAdjustmentPreviewCard` | 直接跟 agent 说 |
| `MilestoneRollbackDialog` | 对话流中任一确认点上的「回到这里」 |
| `TaskProgressCard` | 计划块（渲染 `plan.md`） |
| `PackageChat` + 独立任务面板 | 合并为一条流 |

### 8.2 前端不可退让的三条

1. **助手控制中栏焦点**（`focus` 工具）。缺了它，三栏就是三个互不相干的窗口。
2. **输入框永不锁死。** 阻断性问题时问题卡与自由输入并存。
3. **没有 diff 就没有确认。**

---

## 9. 硬码的下界

"去硬码"如果没有下界，会连不变量一起去掉。以下**必须是代码**，不接受用 prompt 或 Skill 替代：

| 必须硬码 | 规模 | 理由 |
| --- | --- | --- |
| 写入安全 5 条（§3.7） | ~40 行 | 失败静默且不可逆：路径穿越、静默覆盖 |
| 门禁谓词（§4） | ~80 行 | 对客户的承诺，必须可复现、可审计 |
| git 读写 + 候选修订 + 变更组依赖图 + 原子提交 | ~1000 行 | 确认机制的物理基础；部分接受会造成引用断裂 |
| 单写者租约 | ~50 行 | 并发覆盖不可逆 |
| 文档解析（xlsx/docx/pdf） | 328 行 | 模型读不了二进制 |
| 评测管道的机械部分 | 418 行 | 涉及外部系统和真实数据 |
| `who_references` 引用查询原语 | ~60 行 | 模型会漏，且要可复现 |
| 融合的集合运算 | ~60 行 | 同上 |
| 认证、密钥、影子空间隔离 | — | 安全边界 |
| consumer 客户端 | — | 外部协议 |

**判据：涉及安全边界、不可逆操作、对外承诺、或"模型会漏且后果静默"的，硬码；其余交给 agent。**

进程隔离是这条下界的执行机制——agent 进程没有知识包目录写权限，绕不过去（见 [技术选型 §2](04-tech-stack-and-implementation.md)）。

---

## 10. 明确不做

- 不做拖拽式流程图编辑器（渲染审阅 + 结构化编辑已足够）。
- 不做任务图可视化编辑。
- 不做 ETA 与百分比进度。
- 不做 agent 自动接受警告、自动定版本号、自动发布。
- **v1 不做通用校验器。** 只保留 §3.7 的 5 条写入安全 Pre-hook；后续按 §2.3 的判据逐条加回，并记录每条对应哪次真实失败。
- 不做相似度算法、阈值启发式这类"看起来确定其实调不准"的硬码，一律交给 agent 判断。
- 不做敏感数据分级（单独议题，不阻塞本次）。
