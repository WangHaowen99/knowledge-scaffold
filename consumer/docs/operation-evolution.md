# Ontology Operation 进化系统 · 完整方案

## 一、定位与解耦

把"记忆"拆成两类，互不干扰：

| 系统 | 关心什么 | 写入对象 | 谁消费 |
|---|---|---|---|
| **Memory（保留现有）** | 用户偏好、workspace 偏好、系统级习惯 | 普通长期记忆后端（默认 filesystem；OpenViking 可选） | 系统 prompt 注入、跨 session 个性化 |
| **Operation Evolution（新建）** | 某个 ontology operation 的执行流程怎么改进 | Proposal Buffer → operation `.md` | `ontology_context` 召回、operation 自身规范 |

两者数据不互通，进程不耦合。Memory 模块从此不再承载 procedure/pitfall/schema 类内容，反思系统的输出全部改向 Buffer。

## 二、目标

让 ontology 中的 operation 随系统运行不断自我优化：执行更快、更稳、更贴合智能体实际行为。优化由"使用中产生 → 中间缓冲沉淀 → 达标后回写原文"三步驱动，避免单次反思直接污染 spec。

## 三、核心概念

- **Proposal**：对某个 operation 的一条改进意见
  - `procedure` — 可复用的流程模板
  - `pitfall` — 踩过的坑
  - `schema` — 实体字段/格式订正
- **Buffer**：proposal 暂存区，按 (ontology, operation) 分桶，按内容指纹聚合并维护指标
- **Patch**：buffer 中达到阈值的条目批量合并进原 operation `.md`，经审批后落盘

## 四、三步进化流程

### Step 1 · Capture：产生建议

| 通路 | 触发 | source |
|---|---|---|
| **主动·聊天内** | 智能体调用 `memory` 工具捕获用户明确要求沉淀的可复用信息，后台路由到 operation proposal | `active_chat` |
| **被动·后台反思** | 每个 turn 完成后 reflection_worker 自动分析，提炼三类输出 | `passive` |

主动通路直落 buffer，不二次确认；误录由可视化页清理。
被动反思不再写普通长期记忆后端，统一进 buffer。

### Step 2 · Buffer：累加与指标

**存储位置**：`.onto-runtime/operations-evolution/workspaces/<workspace_id>/<onto>/<op>/`
- `proposals.jsonl` — append-only 原始日志
- `aggregate.json` — 按 fingerprint 聚合的指标快照
- `applied/<fp>.json` — 已 patch 入库的归档

刻意脱离 `memory/<user>/` 目录，强调"operation 进化是 workspace/ontology 资产，不是用户私有记忆"。

**Aggregate 关键指标**：
```
occurrences            聚合命中次数
active_endorsements    用户 👍 命中次数
applied_count          ★ 被实际召回且 turn 成功 的次数
rejection_count        用户 👎 命中次数
status                 pending | promoted | rejected | applied
```

**效果闭环（脊梁）**：
`near_point_recall` 注入历史经验时返回 used_fingerprints → orchestrator turn 结束时按成败 + 用户投票调用 `buffer.record_outcome / record_vote` 反向更新指标。
没有这条闭环，"高频"和"真有效"分不开，buffer 退化为噪声放大器。

**RunFeedback（👍/👎）的角色**：
不产生新 proposal，只对本轮 used_fingerprints 投票。无召回则不入 buffer。按钮直接渲染在每个 turn 最终消息气泡下方，三态切换、无弹窗。

### Step 3 · Patch：阈值触发批量回写

**触发模型 —— 事件驱动 + 兜底巡检（与 turn 完全解耦）**

1. **事件驱动（主路径）**：buffer 状态变化时入队
   - `append / record_outcome / record_vote / endorse / edit / applied` 任一发生 → 检查受影响 op 是否可能跨阈值 → 入 patcher 队列
   - patcher 是独立后台 worker，消费队列逐 op 跑 scan
2. **兜底巡检**：每 30 分钟扫一次 buffer，捡漏事件丢失或冷却到期才能触发的情况
3. **手动触发**：管理页"立刻重新评估此 op"按钮

orchestrator 负责 `buffer.record_outcome(used_fps, success)` 和初始化 patcher；patcher 后续靠 buffer 事件、巡检、手动重评运行。

**事件驱动的三道闸门**（避免每次写入都触发昂贵 scan）：

```
buffer 写入
    ↓
① Buffer 预筛（纯内存，纳秒级）
   - 影响的 fingerprint 还是 pending 吗？
   - 指标变化是否可能跨阈值？
   - 该 op 在队列里已有未消费事件了吗？
    ↓ 通过
② 队列去重（按 op 维度合并）
    ↓
Worker 取出 → scan(op)
    ↓
③ scan 内快速 return（文件 stat、字典比对，毫秒级）
   - staged_exists / cooldown / .md 被改过 / 阈值不满足
    ↓ 全过
   调 task_llm 生成 staged（唯一花钱的步骤）
```

**触发条件**（全部 AND）：
```
occurrences ≥ 3
applied_count ≥ 2          ← 真实有效性硬门槛
rejection_count == 0
原 .md 自上次扫描未被人改过
距上次 patch ≥ cooldown
未存在该 op 的 staged 文件
```

**主动来源直通**：`source=active_chat` 且未被 reject 时，`min_applied=0 / min_occurrences=1`。

**Patch 生成与审批**：
1. patcher worker 满足条件 → task_llm 合并达标 proposal
   - schema → 改输入/输出表
   - procedure → 改 `# 逻辑描述`
   - pitfall → 追加 `# 注意事项`
2. 写到 `<op>.md.staged`，默认人工审批
3. **Apply** → 原文件归档到 `applied/<op>-<ts>.md`、覆盖落盘、`learned_version+1`、`last_patched_at` 更新、源 proposal 转 `applied`
4. **Reject** → staged 删除，覆盖的 proposal 默认转 `rejected`

**冷却 (`patch_cooldown_sec`) 的作用**：
- 防抖动：刚 apply 的 promoted 内容立即被召回会让 applied_count 飙升再次触发
- 给审批留窗口、降低 LLM 成本、稳定性观察
- 只挡 patch，不挡 buffer 累计

**staged 已存在时新 proposal 达标怎么办**：
- patcher.scan 立即返回，**绝不覆盖**待审文件
- 新 proposal 照常 append，aggregate 照常更新，status 仍 pending
- 审批后下一次 scan 自然把它们纳入下一批

**冲突保护**：人工已改 `.md`（mtime 变化）→ 转人工 review，patcher 永不覆盖；patcher 永不动 inputs/outputs 主键名，仅改子项 schema。

## 五、新增工具

主动通路唯一入口：`memory`。它捕获用户明确要求未来复用的信息，后台根据内容和上下文路由到普通长期记忆或 operation-evolution buffer。

```
memory(
  content: str
) -> { stored: bool, destination: str, ... }
```

callers：`main`。

**为什么必须是 tool**：
- 对话中产生的改进意图需要结构化落地路径
- tool 调用天然带审计（谁、哪个 run、哪个 turn、对哪个 op）
- 走 active_chat 直通通道（min_occurrences=1, min_applied=0）

**与被动反思的关系**：两者汇入同一个 buffer，同一个 fingerprint 算法。如果用户说的内容碰巧和反思捕到的一致，会聚合到同一个 aggregate 上，`active_endorsements` 自然加权。

**不需要的 tool**：
- ❌ `endorse_proposal` / `reject_proposal` —— UI 操作，走 HTTP API
- ❌ `apply_patch` —— admin 审批动作，绝不能让智能体自己触发
- ❌ `read_buffer` —— 智能体不应感知 buffer，"记忆"由 near_point_recall 自动注入

## 六、用户可见界面

| 界面 | 作用 |
|---|---|
| **聊天内 RunFeedback** | 每个 turn 最终消息气泡下方两个图标 👍/👎，对召回 memory 投票，可切换可撤回，无文本框、无新页面 |
| **OperationProposals 列表页** | 某 op 下所有 pending proposal + 指标（频次/applied/来源）。支持 endorse / reject / edit。`active_chat` 且 `applied_count=0` 加显眼标记。顶部 banner 提示是否有待审 staged |
| **StagedPatchReview 审批页** | markdown diff 视图（左原 / 右新）+ apply / reject / edit-then-apply。顶部导航红点显示跨 op 待审数 |

所有"是否达标、能否 apply"由后端返回 `actionable: bool + reason`，前端只渲染。

## 七、配置项（集中管理）

```
proposal_min_occurrences = 3
proposal_min_applied = 2
proposal_max_rejection = 0
passive_reflection_enabled = false
patch_auto_apply = false
patch_cooldown_sec = 3600
patcher_sweep_interval_sec = 1800   # 兜底巡检
```

`passive_reflection_enabled` 控制是否启用 turn 结束后的后台 passive reflection；关闭后不生成 passive proposal，active_chat 仍然走语义合并。

## 八、安全与回滚

- 每次 apply 写全量快照 `applied/<op>-<ts>.md`，UI 提供"回滚到上一版本"
- operation `.md` frontmatter 加 `learned_version: int` 和 `last_patched_at`
- buffer 写入用文件锁；jsonl 损坏时以 aggregate.json 为真相源，可重建
- staged apply 需 admin 角色

## 九、与现有系统的关系

| 现有模块 | 调整 |
|---|---|
| `MemoryManager` backends | 保留，仅承载用户/workspace/系统偏好；不接收 procedure/pitfall/schema |
| `evolution/reflection_worker.py` | snapshot → LLM → 三类输出流程保留；末端通过共享 proposal writer 做语义合并后写入 buffer |
| `evolution/near_point_recall.py` | 数据源使用 buffer 的 pending/promoted/applied 内容；返回 `used_fingerprints` |
| `ontology_context` | 读取 operation/object 文档时追加 near-point recall，并把 used_fingerprints 写入 ToolContext 供 orchestrator 收尾用 |
| `orchestrator` | 初始化 patcher；turn 成功结束调用 `buffer.record_outcome`；feedback 路由调用 `buffer.record_vote` |
| `RunFeedback.jsx` | 极简两按钮，渲染在最终消息气泡下方 |
| 新增 patcher worker | 独立后台进程/线程，事件队列 + 30min 巡检 + 手动触发，参考 `reflection_worker` 模式 |

## 十、数据流图

### 全景图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用 户 对 话 层                                    │
└─────────────────────────────────────────────────────────────────────────────┘
            │                        │                          │
            │ 发起 turn               │ 点 👍/👎                  │ UI 操作
            ▼                        ▼                          ▼
┌─────────────────────┐   ┌──────────────────┐   ┌──────────────────────────┐
│   Orchestrator      │   │  Feedback Route  │   │ Proposals/Staged 管理页  │
│   (turn lifecycle)  │   │                  │   │ endorse/reject/edit/apply│
└─────────────────────┘   └──────────────────┘   └──────────────────────────┘
            │                        │                          │
            ▼                        │                          │
┌─────────────────────────────────┐  │                          │
│      Jiuwen ReAct Loop          │  │                          │
│      (LLM ↔ tools 循环)         │  │                          │
└─────────────────────────────────┘  │                          │
       │                  │          │                          │
       │ 调用             │ 调用     │                          │
       ▼                  ▼          │                          │
┌──────────────┐  ┌──────────────────────────┐                  │
│ ontology_    │  │ memory                   │  ← 统一记忆入口   │
│ search       │  │                          │                  │
└──────────────┘  └──────────────────────────┘                  │
       │                       │                                │
       │ 注入历史经验           │ source=active_chat              │
       │ + 返回 used_fps        │                                │
       ▼                       │                                │
┌──────────────┐                │                                │
│ near_point_  │                │                                │
│ recall       │                │                                │
└──────────────┘                │                                │
       │                       │                                │
       │ 读 promoted/applied    │                                │
       │                       ▼                                ▼
       │            ┌─────────────────────────────────────────────────┐
       │            │              ★  Proposal Buffer  ★               │
       │            │  .onto-runtime/operations-evolution/workspaces/   │
       │            │  <workspace_id>/<onto>/<op>/                      │
       │            │                                                  │
       │            │   proposals.jsonl    aggregate.json    applied/  │
       │            └─────────────────────────────────────────────────┘
       │                  ▲                          │
       └──────读取────────┘                          │
                                                    │ 状态变化事件（经三道闸门预筛）
                                                    ▼
                              ┌────────────────────────────────────┐
                              │       Patcher Event Queue          │
                              │  (按 op 维度去重)                   │
                              └────────────────────────────────────┘
                                                    │
                                ┌───────────────────┼───────────────────┐
                                │                   │                   │
                          事件驱动主路径       30min 兜底巡检      手动"重评"按钮
                                │                   │                   │
                                ▼                   ▼                   ▼
                              ┌──────────────────────────────────────────┐
                              │       OperationPatcher Worker            │
                              │  (独立后台线程，与 turn 解耦)              │
                              │                                          │
                              │  scan(op):                               │
                              │    if staged_exists / cooldown / 人工改  │
                              │       → return                           │
                              │    if 阈值不满足 → return                │
                              │    task_llm 合并达标 proposal             │
                              │    → 写 <op>.md.staged                   │
                              └──────────────────────────────────────────┘
                                                    │
                                                    ▼
                              ┌──────────────────────────────────────────┐
                              │     <op>.md.staged   (待 admin 审批)      │
                              └──────────────────────────────────────────┘
                                                    │
                                          admin apply / reject
                                                    │
                              ┌──────────┴──────────┬──────────────────┐
                              │ apply               │ reject            │
                              ▼                     ▼                   │
                  ┌───────────────────────┐  ┌────────────────┐         │
                  │ 归档 applied/<op>-<ts> │  │ 删 staged       │         │
                  │ 覆盖 <op>.md           │  │ proposals →    │         │
                  │ learned_version+1     │  │   rejected     │         │
                  │ last_patched_at=now   │  └────────────────┘         │
                  │ proposals → applied   │                              │
                  └───────────────────────┘                              │
                              │                                          │
                              ▼                                          │
                  ┌─────────────────────────┐                            │
                  │  ontology operation     │ ◄── 下次 ontology_context  │
                  │  <op>.md   (已进化)      │     直接读到新版规范        │
                  └─────────────────────────┘                            │


═══════════════════════════════════════════════════════════════════════
                       与 Memory 系统完全解耦
═══════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────┐
│                  Memory System  (保留现有，不变)                      │
│                                                                     │
│   只承载：用户偏好 / workspace 偏好 / 系统级习惯                       │
│   存储：  普通长期记忆后端（默认 filesystem；OpenViking 可选）          │
│   消费：  系统 prompt 注入、跨 session 个性化                          │
│                                                                     │
│   ❌ 不再接收 procedure / pitfall / schema 类内容                     │
│   ❌ 与 buffer / patcher / operation .md 无任何数据通道               │
└─────────────────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════
                       turn 结束的反向闭环（脊梁）
═══════════════════════════════════════════════════════════════════════

  Jiuwen ReAct loop 一个 turn 跑完
        │
        │ 收集本轮 ontology_context near-point recall 的 used_fingerprints
        │ run 成功完成后按 success=True 记录 outcome
        ▼
  Orchestrator.turn_end()
        │
        ├──► buffer.record_outcome(used_fps, success)
        │      ├─ success → applied_count++
        │      └─ fail    → 不计 applied_count
        │
        └──► reflection_worker (异步)
               │
               │ 分析 execution_summary / tool_events
               │ LLM 提炼 procedure/pitfall/schema
               │ 与同 scope 候选做 duplicate/supersedes/refines/conflicts 判断
               ▼
            buffer.append(source=passive)


  用户随后点 👍/👎
        │
        ▼
  Feedback Route
        └──► buffer.record_vote(used_fps, vote)
               ├─ 👍 → active_endorsements++
               └─ 👎 → rejection_count++

  以上任一写入都经过三道闸门预筛后才可能向 Patcher Event Queue 发信号
```

### 关键路径速查

**路径 A · 主动建议（用户对话明确要求改进）**
```
用户说"记住：这步应该先 X 再 Y"
  → LLM 调用 memory
  → 后台路由到 operation_evolution
  → 与同 scope 候选做语义关系判断
  → buffer.append(source=active_chat)
  → 触发 patcher 事件（直通快速通道：min_occurrences=1）
  → 生成 staged
  → admin 审批 apply
  → operation.md 更新
```

**路径 B · 被动沉淀（系统自动学习）**
```
turn 结束
  → reflection_worker 异步分析
  → 与同 scope 候选做语义关系判断
  → buffer.append(source=passive)
  → 暂时不达标，继续累计
  → 后续 turn 召回此 fingerprint 且成功
  → buffer.record_outcome → applied_count++
  → 跨过阈值 → 触发 patcher
  → 生成 staged → admin 审批
```

**路径 C · 用户投票（强化/否决本轮召回的历史经验）**
```
用户在最终消息下点 👍
  → buffer.record_vote(used_fps, up)
  → active_endorsements++
  → 可能跨阈值 → 触发 patcher

用户点 👎
  → rejection_count++
  → 永久挡住该 fingerprint 进 staged
```

### 数据写入权责（单一写入源原则）

| 文件/资源 | 唯一写入者 |
|---|---|
| `proposals.jsonl` / `aggregate.json` | Buffer 模块 |
| `<op>.md.staged` | Patcher Worker |
| `<op>.md`（覆盖） | Admin 审批动作 |
| `applied/<op>-<ts>.md`（快照） | Admin 审批动作 |
| Memory store | 普通长期记忆后端（与本系统无交集） |

### 解耦边界

- **Memory ⊥ Buffer**：两套存储、两套生命周期、两套消费者，无任何数据通道
- **Patcher ⊥ Turn**：patcher 由 buffer 状态变化驱动，orchestrator 不知道 patcher 存在
- **前端 ⊥ 阈值**：所有 actionable 判断在后端，前端只渲染 `actionable: bool + reason`

## 十一、关键设计取舍

1. **applied_count 是脊梁** —— 唯一区分"高频"与"真有效"的信号
2. **进化与 memory 解耦** —— 两套数据、两套生命周期，互不污染
3. **patcher 与 turn 解耦** —— 由 buffer 状态变化驱动，orchestrator 不知道 patcher 存在
4. **主动通路直落不二次确认** —— 换流畅体验，靠可视化页清理误录
5. **buffer 是 operation 进化的单一写入源** —— 杜绝双写漂移
6. **默认人工审批 + 同 op 单 staged** —— 审批模型最简，回写不可逆所以从严
7. **前端不做阈值判断** —— 规则集中后端
8. **三道闸门避免无效 scan** —— buffer 预筛 / 队列去重 / scan 内快速 return，只有过完才调 task_llm
